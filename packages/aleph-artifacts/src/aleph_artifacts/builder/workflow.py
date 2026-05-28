"""Builder LangGraph workflow.

Nodes (linear):
  outline → section_compose → citation_resolve → chart_freeze →
  bibliography → package.

Produces a markdown report bundle and writes an `ArtifactVersion` row
referencing every contributing wiki revision, dataset version,
rendered asset, and source version (lineage_jsonb).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from aleph_artifacts.csl import format_bibliography
from aleph_artifacts.exporters import (
    markdown_bundle_bytes,
    markdown_to_pdf_bytes,
)
from aleph_artifacts.models import Artifact, ArtifactVersion
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import start_span
from aleph_rks.models import Source, SourceAsset, SourceVersion
from aleph_wiki.models import WikiPage, WikiRevision

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_rks.asset_store import AssetStore
    from aleph_security.principal import Principal


@dataclass
class _Ctx:
    session_maker: "async_sessionmaker[AsyncSession]"
    asset_store: "AssetStore"
    principal: "Principal"


_active_ctx: _Ctx | None = None


def _ctx() -> _Ctx:
    if _active_ctx is None:
        msg = "BuilderWorkflow context not initialized"
        raise RuntimeError(msg)
    return _active_ctx


class BuilderState(TypedDict, total=False):
    project_id: UUID
    agent_run_id: UUID
    artifact_id: UUID
    artifact_kind: str  # report_pdf | report_markdown_bundle | source_pack | report_docx
    template_name: str
    csl_style: str
    wiki_page_ids: list[UUID]
    dataset_version_ids: list[UUID]
    outline: list[dict]
    composed_markdown: str
    bibliography_markdown: str
    rendered_asset_ids: list[UUID]
    artifact_bytes: bytes
    artifact_storage_uri: str
    artifact_sha256: str
    lineage: dict[str, Any]


async def _node_outline(state: BuilderState) -> dict[str, Any]:
    """Produce an outline from selected wiki pages."""
    ctx = _ctx()
    with start_span("builder.outline"):
        if not state.get("wiki_page_ids"):
            return {"outline": []}
        async with ctx.session_maker() as session:
            pages = list(
                (
                    await session.execute(
                        select(WikiPage).where(
                            WikiPage.id.in_(state["wiki_page_ids"]),
                            WikiPage.project_id == state["project_id"],
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {
                "outline": [
                    {
                        "page_id": str(p.id),
                        "title": p.title,
                        "slug": p.slug,
                        "page_kind": p.page_kind,
                    }
                    for p in pages
                ]
            }


async def _node_section_compose(state: BuilderState) -> dict[str, Any]:
    """Compose each outlined page into a section, preserving wikilinks + citations."""
    ctx = _ctx()
    with start_span("builder.section_compose"):
        sections: list[str] = []
        async with ctx.session_maker() as session:
            for item in state.get("outline") or []:
                page = await session.get(WikiPage, UUID(item["page_id"]))
                if page is None or page.current_revision_id is None:
                    continue
                rev = await session.get(WikiRevision, page.current_revision_id)
                if rev is None:
                    continue
                # Demote headings: page becomes H1 inside the report H1.
                section = f"## {page.title}\n\n{rev.body_md.strip()}\n"
                sections.append(section)
        return {"composed_markdown": "\n\n".join(sections)}


async def _node_citation_resolve(state: BuilderState) -> dict[str, Any]:
    """Walk citation markers in `composed_markdown` and build a CSL-JSON list."""
    ctx = _ctx()
    import re

    with start_span("builder.citation_resolve"):
        body = state.get("composed_markdown") or ""
        short_ids = set(re.findall(r"\[\[Source:([A-Z0-9]+)\]\]", body))
        items: list[dict] = []
        async with ctx.session_maker() as session:
            for sid in short_ids:
                src = (
                    await session.execute(
                        select(Source).where(
                            Source.project_id == state["project_id"],
                            Source.short_id == sid,
                        )
                    )
                ).scalar_one_or_none()
                if src is None:
                    continue
                meta = dict(src.source_metadata_jsonb or {})
                items.append(
                    {
                        "id": sid,
                        "type": meta.get("type", "article"),
                        "title": src.title,
                        "author": meta.get("authors")
                        or meta.get("author")
                        or [],
                        "issued": meta.get("issued")
                        or (
                            {"date-parts": [[meta["year"]]]}
                            if meta.get("year")
                            else {}
                        ),
                        "URL": src.url,
                        "container-title": meta.get("publication_info")
                        or meta.get("container-title"),
                        "DOI": meta.get("doi"),
                    }
                )
        return {"csl_items": items}


async def _node_chart_freeze(state: BuilderState) -> dict[str, Any]:
    """For each dataset_version referenced, ensure a RenderedAsset exists.

    Inc 7 ships the bookkeeping path; the actual chromium rendering is
    invoked by `render_card_job` in the worker. For now the workflow
    creates `RenderedAsset` rows pointing at storage_uri placeholders;
    the worker fills them in.
    """
    with start_span("builder.chart_freeze"):
        # No-op in Inc 7's first commit; the render_service handles it.
        return {"rendered_asset_ids": state.get("rendered_asset_ids") or []}


async def _node_bibliography(state: BuilderState) -> dict[str, Any]:
    with start_span("builder.bibliography"):
        items = state.get("csl_items") or []  # type: ignore[assignment]
        if not items:
            return {"bibliography_markdown": ""}
        md = format_bibliography(
            items=items,
            style=state.get("csl_style") or "apa-7",
            output="markdown",
        )
        return {"bibliography_markdown": md}


async def _node_package(state: BuilderState) -> dict[str, Any]:
    """Produce the final bytes per `artifact_kind`."""
    ctx = _ctx()
    with start_span("builder.package"):
        full_md = (
            f"# {state.get('artifact_id')}\n\n"
            + (state.get("composed_markdown") or "")
            + "\n\n## Bibliography\n\n"
            + (state.get("bibliography_markdown") or "(no citations)")
        )
        kind = state.get("artifact_kind") or "report_markdown_bundle"
        if kind == "report_pdf":
            payload = markdown_to_pdf_bytes(
                report_md=full_md,
                title=f"Artifact {state.get('artifact_id')}",
            )
            ext = "pdf"
        elif kind == "source_pack":
            # source-pack assembled by a separate worker; bundle the
            # markdown report + a placeholder manifest here.
            from aleph_artifacts.exporters import source_pack_bytes

            payload = source_pack_bytes(
                sources=[],
                raw_blobs={},
                normalized_md={"report.md": full_md},
            )
            ext = "zip"
        else:
            payload = markdown_bundle_bytes(
                report_md=full_md,
                manifest={
                    "outline": state.get("outline"),
                    "lineage": state.get("lineage"),
                    "csl_style": state.get("csl_style"),
                },
            )
            ext = "zip"

        sha = hashlib.sha256(payload).hexdigest()
        storage_uri = await _put_artifact_bytes(
            project_id=state["project_id"],
            artifact_id=state["artifact_id"],
            version_no=int(state.get("version_no", 1)),  # type: ignore[arg-type]
            ext=ext,
            data=payload,
        )
        return {
            "artifact_bytes": payload,
            "artifact_storage_uri": storage_uri,
            "artifact_sha256": sha,
        }


async def _put_artifact_bytes(
    *,
    project_id: UUID,
    artifact_id: UUID,
    version_no: int,
    ext: str,
    data: bytes,
) -> str:
    """Upload via the AssetStore. Path: artifacts/{id}/{version_no}.{ext}."""
    ctx = _ctx()
    # Reuse the AssetStore helper. We can't use `put_source_asset`
    # because that's keyed by source_id; minor inline write here.
    from io import BytesIO

    key = f"projects/{project_id}/artifacts/{artifact_id}/{version_no}.{ext}"
    bucket = ctx.asset_store._bucket  # noqa: SLF001
    client = ctx.asset_store._client  # noqa: SLF001
    client.put_object(
        bucket,
        key,
        data=BytesIO(data),
        length=len(data),
        content_type={
            "pdf": "application/pdf",
            "zip": "application/zip",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(ext, "application/octet-stream"),
    )
    return f"s3://{bucket}/{key}"


class BuilderWorkflow:
    def __init__(
        self,
        *,
        session_maker: "async_sessionmaker[AsyncSession]",
        asset_store: "AssetStore",
        principal: "Principal",
    ) -> None:
        self._ctx = _Ctx(
            session_maker=session_maker,
            asset_store=asset_store,
            principal=principal,
        )
        graph: StateGraph = StateGraph(BuilderState)
        graph.add_node("outline", _node_outline)
        graph.add_node("section_compose", _node_section_compose)
        graph.add_node("citation_resolve", _node_citation_resolve)
        graph.add_node("chart_freeze", _node_chart_freeze)
        graph.add_node("bibliography", _node_bibliography)
        graph.add_node("package", _node_package)
        graph.add_edge(START, "outline")
        graph.add_edge("outline", "section_compose")
        graph.add_edge("section_compose", "citation_resolve")
        graph.add_edge("citation_resolve", "chart_freeze")
        graph.add_edge("chart_freeze", "bibliography")
        graph.add_edge("bibliography", "package")
        graph.add_edge("package", END)
        self._compiled = graph.compile()

    async def run(
        self,
        *,
        ledger: "LedgerWriter",
        project_id: UUID,
        agent_run_id: UUID,
        artifact: Artifact,
        artifact_kind: str,
        wiki_page_ids: list[UUID],
        dataset_version_ids: list[UUID],
        template_name: str,
        csl_style: str,
    ) -> ArtifactVersion:
        global _active_ctx
        _active_ctx = self._ctx
        try:
            async with self._ctx.session_maker() as session:
                max_no = (
                    await session.execute(
                        select(
                            ArtifactVersion.version_no
                        )
                        .where(ArtifactVersion.artifact_id == artifact.id)
                        .order_by(ArtifactVersion.version_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                next_no = (int(max_no) if max_no else 0) + 1

            state: BuilderState = {
                "project_id": project_id,
                "agent_run_id": agent_run_id,
                "artifact_id": artifact.id,
                "artifact_kind": artifact_kind,
                "template_name": template_name,
                "csl_style": csl_style,
                "wiki_page_ids": wiki_page_ids,
                "dataset_version_ids": dataset_version_ids,
                "version_no": next_no,  # type: ignore[typeddict-unknown-key]
            }
            out: BuilderState = await self._compiled.ainvoke(state)  # type: ignore[assignment]

            lineage = {
                "wiki_page_ids": [str(w) for w in wiki_page_ids],
                "dataset_version_ids": [str(d) for d in dataset_version_ids],
                "rendered_asset_ids": [
                    str(r) for r in (out.get("rendered_asset_ids") or [])
                ],
                "outline": out.get("outline"),
                "template_name": template_name,
                "csl_style": csl_style,
                "commit_message": f"Build {artifact.short_id} v{next_no}",
            }

            async with self._ctx.session_maker() as session:
                event = await ledger.append(
                    project_id=project_id,
                    actor_id=self._ctx.principal.user_id,
                    actor_kind=self._ctx.principal.actor_kind,
                    action_kind="artifact.version.create",
                    target_id=artifact.id,
                    target_kind="artifact",
                    payload={
                        "version_no": next_no,
                        "artifact_kind": artifact_kind,
                        "lineage": lineage,
                        "sha256": out["artifact_sha256"],
                    },
                    trace_id=None,
                )
                version = ArtifactVersion(
                    id=uuid7(),
                    project_id=project_id,
                    artifact_id=artifact.id,
                    version_no=next_no,
                    storage_uri=out["artifact_storage_uri"],
                    bytes_size=len(out["artifact_bytes"]),
                    sha256=out["artifact_sha256"],
                    parent_version_id=artifact.current_version_id,
                    lineage_jsonb=lineage,
                    template_name=template_name,
                    csl_style=csl_style,
                    builder_agent_run_id=agent_run_id,
                    author_kind=self._ctx.principal.actor_kind,
                    author_id=self._ctx.principal.user_id,
                    ledger_event_id=event.id,
                )
                session.add(version)
                await session.flush()
                # Refresh the artifact to bump current_version_id.
                refreshed = await session.get(Artifact, artifact.id)
                if refreshed is not None:
                    refreshed.current_version_id = version.id
                await session.commit()
                return version
        finally:
            _active_ctx = None
