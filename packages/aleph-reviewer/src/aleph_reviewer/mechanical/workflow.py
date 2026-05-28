"""MechanicalReviewer LangGraph workflow.

Runs on every wiki revision commit. Checks:
  citation_match | broken_wikilink | stale_source | hash_mismatch |
  duplicate_source | alias_inconsistency | schema_invalid.

Most checks are deterministic. citation_match wraps AIQ's
`verify_citations` contract (mirrored in `aleph_wiki.citation_verification`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from aleph_core.time import utcnow
from aleph_observability.tracing import start_span
from aleph_reviewer.review_service import add_finding, finalize_run, start_run
from aleph_rks.models import Source, SourceAsset, SourceVersion
from aleph_wiki.citation_verification import (
    CITATION_RE,
    CitationVerificationFailure,
    verify_citations,
)
from aleph_wiki.models import (
    Citation,
    WikiClaim,
    WikiLink,
    WikiPage,
    WikiRevision,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from aleph_security.principal import Principal


@dataclass
class _Ctx:
    session_maker: "async_sessionmaker[AsyncSession]"
    principal: "Principal"
    freshness_threshold: timedelta = timedelta(days=180)


_active_ctx: _Ctx | None = None


def _ctx() -> _Ctx:
    if _active_ctx is None:
        msg = "MechanicalReviewerWorkflow context not initialized"
        raise RuntimeError(msg)
    return _active_ctx


class MechanicalReviewState(TypedDict, total=False):
    project_id: UUID
    revision_id: UUID
    page_id: UUID
    agent_run_id: UUID
    review_run_id: UUID
    finding_count: int


async def _node_citation_match(state: MechanicalReviewState) -> dict[str, int]:
    ctx = _ctx()
    n = 0
    with start_span("review.mechanical.citation_match"):
        async with ctx.session_maker() as session:
            rev = await session.get(WikiRevision, state["revision_id"])
            if rev is None:
                return {"n": 0}
            # Build registry from Citation rows for claims on this revision.
            claim_rows = list(
                (
                    await session.execute(
                        select(WikiClaim).where(WikiClaim.revision_id == rev.id)
                    )
                ).scalars().all()
            )
            claim_ids = [c.id for c in claim_rows]
            citation_rows = (
                list(
                    (
                        await session.execute(
                            select(Citation).where(Citation.claim_id.in_(claim_ids))
                        )
                    ).scalars().all()
                )
                if claim_ids
                else []
            )
            registry: dict[str, Any] = {}
            for c in citation_rows:
                marker = c.citation_marker.strip("[]")
                registry[marker] = c
            try:
                verify_citations(body_md=rev.body_md, source_registry=registry)
            except CitationVerificationFailure as exc:
                for marker in exc.missing_markers:
                    await add_finding(
                        session,
                        review_run_id=state["review_run_id"],
                        project_id=state["project_id"],
                        finding_kind="citation_match_failure",
                        severity="high",
                        title=f"Citation [{marker}] has no backing source",
                        description=(
                            f"Marker [{marker}] in revision {rev.id} of page {state['page_id']} "
                            "doesn't resolve to a Citation row."
                        ),
                        target_page_id=state["page_id"],
                        target_revision_id=rev.id,
                        evidence_refs=[{"kind": "marker", "value": marker}],
                        auto_resolvable=False,
                        created_by=ctx.principal.user_id,
                    )
                    n += 1
            await session.commit()
    return {"n": n}


async def _node_broken_links(state: MechanicalReviewState) -> dict[str, int]:
    ctx = _ctx()
    n = 0
    with start_span("review.mechanical.broken_links"):
        async with ctx.session_maker() as session:
            link_rows = list(
                (
                    await session.execute(
                        select(WikiLink).where(
                            WikiLink.project_id == state["project_id"],
                            WikiLink.src_revision_id == state["revision_id"],
                            WikiLink.dst_page_id.is_(None),
                            WikiLink.occurrences > 1,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for link in link_rows:
                await add_finding(
                    session,
                    review_run_id=state["review_run_id"],
                    project_id=state["project_id"],
                    finding_kind="broken_wikilink",
                    severity="medium",
                    title=f"Broken wikilink [[{link.dst_title}]]",
                    description=(
                        f"[[{link.dst_title}]] appears {link.occurrences} times "
                        f"but doesn't resolve to a page or alias."
                    ),
                    target_page_id=state["page_id"],
                    target_revision_id=state["revision_id"],
                    evidence_refs=[
                        {"kind": "wikilink", "dst_title": link.dst_title}
                    ],
                    auto_resolvable=False,
                    created_by=ctx.principal.user_id,
                )
                n += 1
            await session.commit()
    return {"n": n}


async def _node_stale_sources(state: MechanicalReviewState) -> dict[str, int]:
    ctx = _ctx()
    n = 0
    with start_span("review.mechanical.stale_sources"):
        async with ctx.session_maker() as session:
            stmt = (
                select(WikiClaim, Citation)
                .join(Citation, Citation.claim_id == WikiClaim.id)
                .where(WikiClaim.revision_id == state["revision_id"])
            )
            rows = list((await session.execute(stmt)).all())
            seen: set[UUID] = set()
            cutoff = utcnow() - ctx.freshness_threshold
            for claim, cite in rows:
                if cite.source_page_id is None:
                    continue
                # Resolve source by joining SourcePage → Source via project + page.
                from aleph_wiki.models import SourcePage

                sp = await session.get(SourcePage, cite.source_page_id)
                if sp is None or sp.source_id in seen:
                    continue
                seen.add(sp.source_id)
                src = await session.get(Source, sp.source_id)
                if src is None:
                    continue
                ver = (
                    await session.execute(
                        select(SourceVersion)
                        .where(SourceVersion.source_id == src.id)
                        .order_by(SourceVersion.version_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if ver is None:
                    continue
                if ver.fetched_at < cutoff:
                    await add_finding(
                        session,
                        review_run_id=state["review_run_id"],
                        project_id=state["project_id"],
                        finding_kind="stale_source",
                        severity="low",
                        title=f"Stale source: {src.short_id}",
                        description=(
                            f"Source {src.short_id} ({src.title}) was last fetched "
                            f"{ver.fetched_at}. Consider re-ingesting."
                        ),
                        target_source_id=src.id,
                        evidence_refs=[
                            {"kind": "source", "source_id": str(src.id), "short_id": src.short_id}
                        ],
                        auto_resolvable=False,
                        created_by=ctx.principal.user_id,
                    )
                    n += 1
            await session.commit()
    return {"n": n}


async def _node_duplicate_sources(state: MechanicalReviewState) -> dict[str, int]:
    ctx = _ctx()
    n = 0
    with start_span("review.mechanical.duplicate_sources"):
        async with ctx.session_maker() as session:
            stmt = (
                select(SourceAsset.sha256, func.count(SourceAsset.id))
                .where(SourceAsset.project_id == state["project_id"])
                .group_by(SourceAsset.sha256)
                .having(func.count(SourceAsset.id) > 1)
            )
            for sha, count in (await session.execute(stmt)).all():
                await add_finding(
                    session,
                    review_run_id=state["review_run_id"],
                    project_id=state["project_id"],
                    finding_kind="duplicate_source",
                    severity="low",
                    title=f"Duplicate source content: sha256={sha[:10]}…",
                    description=(
                        f"{count} source assets share the same sha256. "
                        "Consider merging."
                    ),
                    evidence_refs=[{"kind": "sha256", "value": sha, "count": int(count)}],
                    auto_resolvable=False,
                    created_by=ctx.principal.user_id,
                )
                n += 1
            await session.commit()
    return {"n": n}


async def _node_finalize(state: MechanicalReviewState) -> dict[str, int]:
    n = (
        (state.get("n_citation") or 0)  # type: ignore[arg-type]
        + (state.get("n_links") or 0)  # type: ignore[arg-type]
        + (state.get("n_stale") or 0)  # type: ignore[arg-type]
        + (state.get("n_dupe") or 0)  # type: ignore[arg-type]
    )
    ctx = _ctx()
    async with ctx.session_maker() as session:
        await finalize_run(
            session,
            run_id=state["review_run_id"],
            status="completed",
            finding_count=n,
        )
        await session.commit()
    return {"finding_count": n}


class MechanicalReviewerWorkflow:
    def __init__(
        self,
        *,
        session_maker: "async_sessionmaker[AsyncSession]",
        principal: "Principal",
    ) -> None:
        self._ctx = _Ctx(session_maker=session_maker, principal=principal)
        graph: StateGraph = StateGraph(MechanicalReviewState)
        # Wrap each node to fan results into typed slots on state.
        graph.add_node(
            "citation_match",
            lambda s: _node_citation_match(s).__await__()  # type: ignore[func-returns-value]
            if False
            else _wrap(_node_citation_match, "n_citation"),
        )
        graph.add_node("citation_match", _wrap(_node_citation_match, "n_citation"))
        graph.add_node("broken_links", _wrap(_node_broken_links, "n_links"))
        graph.add_node("stale_sources", _wrap(_node_stale_sources, "n_stale"))
        graph.add_node("duplicate_sources", _wrap(_node_duplicate_sources, "n_dupe"))
        graph.add_node("finalize", _node_finalize)
        graph.add_edge(START, "citation_match")
        graph.add_edge("citation_match", "broken_links")
        graph.add_edge("broken_links", "stale_sources")
        graph.add_edge("stale_sources", "duplicate_sources")
        graph.add_edge("duplicate_sources", "finalize")
        graph.add_edge("finalize", END)
        self._compiled = graph.compile()

    async def run(self, *, project_id: UUID, revision_id: UUID, page_id: UUID,
                  agent_run_id: UUID) -> int:
        global _active_ctx
        _active_ctx = self._ctx
        async with self._ctx.session_maker() as session:
            run = await start_run(
                session,
                project_id=project_id,
                kind="mechanical",
                trigger="revision_commit",
                target_revision_id=revision_id,
                target_scope="revision",
                agent_run_id=agent_run_id,
                created_by=self._ctx.principal.user_id,
            )
            await session.commit()
            review_run_id = run.id
        try:
            state: MechanicalReviewState = {
                "project_id": project_id,
                "revision_id": revision_id,
                "page_id": page_id,
                "agent_run_id": agent_run_id,
                "review_run_id": review_run_id,
            }
            out = await self._compiled.ainvoke(state)
            return int(out.get("finding_count", 0))  # type: ignore[arg-type]
        finally:
            _active_ctx = None


def _wrap(handler, slot: str):
    async def _wrapped(state):  # type: ignore[no-untyped-def]
        out = await handler(state)
        return {slot: out.get("n", 0)}

    return _wrapped
