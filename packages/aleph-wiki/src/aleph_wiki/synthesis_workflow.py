"""Synthesis-compose workflow.

Consumes an AIQ DeepResearcher report (or any structured research
report shaped the same way) and lands it as wiki draft pages with
`status="draft"`, ready for owner approval.

Workflow nodes:
  concept_normalize → citation_verification → wikilink_resolve →
  wiki_index_update → commit_revision (with status="draft" +
  SynthesisProposal row insertion).

This module deliberately mirrors the Inc 1 ingest workflow's structure
so the same `_active_ctx` pattern works.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.repos.agent_events import with_phase
from aleph_observability.tracing import start_span
from aleph_wiki.alias_service import AliasService
from aleph_wiki.citation_verification import (
    CitationVerificationFailure,
    verify_citations,
)
from aleph_wiki.wiki_service import (
    ClaimDraft,
    CitationDraft,
    WikiLinkDraft,
    WikiService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


@dataclass(frozen=True)
class AIQReportSourceRef:
    source_short_id: str
    title: str
    url: str | None


@dataclass(frozen=True)
class AIQReportClaim:
    text: str
    citation_markers: list[str]  # ["c1", "c3"]
    section_anchor: str | None


@dataclass(frozen=True)
class AIQReport:
    """Structured research report returned by AIQ DeepResearcher."""

    topic: str
    body_md: str
    summary: str
    sources: list[AIQReportSourceRef]
    citations_by_marker: dict[str, AIQReportSourceRef]
    """Map of `c1` / `c2` markers (as they appear in `body_md`) to source refs."""
    claims: list[AIQReportClaim]


@dataclass
class _Ctx:
    session_maker: "async_sessionmaker[AsyncSession]"
    litellm: "LiteLLMClient"
    principal: "Principal"


# ContextVar (not a module global) so concurrent synthesis runs in the same
# worker process (arq max_jobs > 1) don't clobber each other's context.
_active_ctx_var: ContextVar[_Ctx | None] = ContextVar(
    "aleph_synthesis_active_ctx", default=None
)


def _ctx() -> _Ctx:
    ctx = _active_ctx_var.get()
    if ctx is None:
        msg = "SynthesisWorkflow context not initialized"
        raise RuntimeError(msg)
    return ctx


class SynthesisState(TypedDict, total=False):
    agent_run_id: UUID
    project_id: UUID
    topic: str
    aiq_report: AIQReport
    profile_bindings: dict[str, Any]
    normalized_topic: str
    verified_markers: dict[str, AIQReportSourceRef]
    committed_revision_ids: list[UUID]
    proposal_ids: list[UUID]
    verification_failures: list[str]


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


@with_phase("concept_normalize", ctx_getter=lambda: _ctx())
async def _node_concept_normalize(state: SynthesisState) -> dict:
    with start_span(
        "synthesis.node.concept_normalize",
        **{
            "aleph.node": "concept_normalize",
            "aleph.project_id": str(state["project_id"]),
        },
    ):
        ctx = _ctx()
        topic = state["topic"].strip()
        async with ctx.session_maker() as session:
            svc = AliasService(session)
            resolution = await svc.resolve(
                project_id=state["project_id"], surface_form=topic
            )
            normalized = resolution.canonical_name if resolution else topic
        return {"normalized_topic": normalized}


@with_phase("citation_verification", ctx_getter=lambda: _ctx())
async def _node_citation_verification(state: SynthesisState) -> dict:
    """Verify every `[cN]` marker in the report body has a real source ref."""
    with start_span(
        "synthesis.node.citation_verification",
        **{
            "aleph.node": "citation_verification",
            "aleph.project_id": str(state["project_id"]),
        },
    ):
        report = state["aiq_report"]
        try:
            verified = verify_citations(
                body_md=report.body_md,
                source_registry=report.citations_by_marker,
            )
            return {"verified_markers": verified, "verification_failures": []}
        except CitationVerificationFailure as exc:
            return {
                "verified_markers": exc.verified,
                "verification_failures": list(exc.missing_markers),
            }


@with_phase("wikilink_resolve", ctx_getter=lambda: _ctx())
async def _node_wikilink_resolve(state: SynthesisState) -> dict:
    """Resolve `[[wikilink]]` targets in the report body via the alias table."""
    with start_span(
        "synthesis.node.wikilink_resolve",
        **{
            "aleph.node": "wikilink_resolve",
            "aleph.project_id": str(state["project_id"]),
        },
    ):
        ctx = _ctx()
        wikilinks: list[WikiLinkDraft] = []
        counts: dict[str, int] = {}
        for m in WIKILINK_RE.finditer(state["aiq_report"].body_md):
            t = m.group(1).split("|", 1)[0].strip()
            counts[t] = counts.get(t, 0) + 1
        if counts:
            async with ctx.session_maker() as session:
                svc = AliasService(session)
                for title, occ in counts.items():
                    r = await svc.resolve(
                        project_id=state["project_id"], surface_form=title
                    )
                    wikilinks.append(
                        WikiLinkDraft(
                            dst_title=title,
                            dst_page_id=r.canonical_page_id if r else None,
                            occurrences=occ,
                        )
                    )
        return {"resolved_wikilinks": wikilinks}


@with_phase("commit_revision", ctx_getter=lambda: _ctx())
async def _node_commit_revision(state: SynthesisState) -> dict:
    """Commit a single synthesis page as a draft and record the SynthesisProposal."""
    with start_span(
        "synthesis.node.commit_revision",
        **{
            "aleph.node": "commit_revision",
            "aleph.project_id": str(state["project_id"]),
        },
    ):
        ctx = _ctx()
        # Verification failures block commit per spec §3.9.
        failures = state.get("verification_failures") or []
        if failures:
            msg = (
                "synthesis commit blocked: citations failed verification: "
                + ", ".join(failures[:8])
            )
            raise CitationVerificationFailure(
                msg, verified=state.get("verified_markers") or {}, missing_markers=failures
            )

        report = state["aiq_report"]
        wikilinks = state.get("resolved_wikilinks") or []  # type: ignore[assignment]

        claim_drafts: list[ClaimDraft] = []
        for c in report.claims:
            citations = []
            for marker_raw in c.citation_markers:
                marker = (
                    marker_raw if marker_raw.startswith("[") else f"[{marker_raw}]"
                )
                ref = report.citations_by_marker.get(marker.strip("[]"))
                if ref is None:
                    continue
                citations.append(
                    CitationDraft(
                        chunk_ids=[],
                        source_page_id=None,  # source page resolved by short_id later
                        citation_marker=marker[:16],
                    )
                )
            claim_drafts.append(
                ClaimDraft(
                    text=c.text,
                    confidence="cited" if citations else "uncited",
                    section_anchor=c.section_anchor,
                    citations=citations,
                )
            )

        async with ctx.session_maker() as session:
            from aleph_connectors.models import SynthesisProposal
            from aleph_db.repos.ledger import LedgerWriter

            ledger = LedgerWriter(session)
            svc = WikiService(session)
            result = await svc.commit_revision(
                principal=ctx.principal,
                ledger=ledger,
                project_id=state["project_id"],
                page_id=None,
                title=state.get("normalized_topic") or report.topic,
                slug=None,
                page_kind="synthesis",
                body_md=report.body_md,
                summary=report.summary[:2048],
                claims=claim_drafts,
                wikilinks=wikilinks,
                commit_message=f"Synthesis: {state.get('normalized_topic') or report.topic}",
                respect_hand_edits=True,
            )

            # Pages land as 'draft' status until approved. wiki_service
            # currently always writes 'draft' on new pages (matches our need).
            proposal = SynthesisProposal(
                id=uuid7(),
                project_id=state["project_id"],
                page_id=result.page_id,
                revision_id=result.revision_id,
                agent_run_id=state["agent_run_id"],
                topic=(state.get("normalized_topic") or report.topic)[:512],
                status="pending",
                approval_decision_id=None,
                created_by=ctx.principal.user_id,
                access_scope="project",
            )
            session.add(proposal)
            await session.flush()

            await ledger.append(
                project_id=state["project_id"],
                actor_id=ctx.principal.user_id,
                actor_kind=ctx.principal.actor_kind,
                action_kind="synthesis.proposal.create",
                target_id=proposal.id,
                target_kind="synthesis_proposal",
                payload={
                    "agent_run_id": str(state["agent_run_id"]),
                    "page_id": str(result.page_id),
                    "revision_id": str(result.revision_id),
                    "topic": proposal.topic,
                    "source_count": len(report.sources),
                },
                trace_id=None,
            )
            await session.commit()
            return {
                "committed_revision_ids": [result.revision_id],
                "proposal_ids": [proposal.id],
            }


@with_phase("wiki_index_update", ctx_getter=lambda: _ctx())
async def _node_wiki_index_update(state: SynthesisState) -> dict:
    # IndexService.refresh_page was already called by commit_revision.
    return {}


class SynthesisWorkflow:
    def __init__(
        self,
        *,
        session_maker: "async_sessionmaker[AsyncSession]",
        litellm: "LiteLLMClient",
        principal: "Principal",
    ) -> None:
        self._ctx = _Ctx(
            session_maker=session_maker, litellm=litellm, principal=principal
        )
        graph: StateGraph = StateGraph(SynthesisState)
        graph.add_node("concept_normalize", _node_concept_normalize)
        graph.add_node("citation_verification", _node_citation_verification)
        graph.add_node("wikilink_resolve", _node_wikilink_resolve)
        graph.add_node("commit_revision", _node_commit_revision)
        graph.add_node("wiki_index_update", _node_wiki_index_update)
        graph.add_edge(START, "concept_normalize")
        graph.add_edge("concept_normalize", "citation_verification")
        graph.add_edge("citation_verification", "wikilink_resolve")
        graph.add_edge("wikilink_resolve", "commit_revision")
        graph.add_edge("commit_revision", "wiki_index_update")
        graph.add_edge("wiki_index_update", END)
        self._compiled = graph.compile()

    async def run(self, initial: SynthesisState) -> SynthesisState:
        token = _active_ctx_var.set(self._ctx)
        try:
            out = await self._compiled.ainvoke(initial)
            return out  # type: ignore[return-value]
        finally:
            _active_ctx_var.reset(token)
