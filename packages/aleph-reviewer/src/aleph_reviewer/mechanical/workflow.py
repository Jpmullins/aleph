"""MechanicalReviewer LangGraph workflow.

Runs on every wiki revision commit. Checks:
  citation_match | fabricated_doi | retracted_source | broken_wikilink |
  stale_source | hash_mismatch | duplicate_source | alias_inconsistency |
  schema_invalid.

Most checks are deterministic. citation_match uses
`aleph_wiki.citation_verification.verify_citations`.
doi_verification wraps `aleph_scholar` DOI verification (spec WP-2 §6).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypedDict
from uuid import UUID

import structlog
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from aleph_core.time import utcnow
from aleph_db.repos.agent_events import with_phase
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability import current_trace_id
from aleph_observability.tracing import start_span
from aleph_reviewer.retraction import retract_source
from aleph_reviewer.review_service import add_finding, finalize_run, start_run
from aleph_rks.models import Source, SourceAsset, SourceVersion
from aleph_scholar import ScholarUpstreamError, extract_dois, normalize_doi
from aleph_wiki.citation_verification import (
    CitationVerificationFailure,
    verify_citations,
)
from aleph_wiki.models import (
    Citation,
    SourcePage,
    WikiClaim,
    WikiLink,
    WikiRevision,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_scholar import DoiVerdict
    from aleph_security.principal import Principal

_log = structlog.get_logger(__name__)

#: Max DOIs verified per review run (spec WP-2 §6) — the tail is skipped
#: with a note appended to any emitted finding descriptions.
DOI_VERIFY_CAP = 50


class SupportsVerifyDois(Protocol):
    """The slice of `aleph_scholar.ScholarService` the reviewer consumes.

    Injectable on `MechanicalReviewerWorkflow` so unit tests stub the
    verification without any network access.
    """

    async def verify_dois(self, dois: list[str]) -> list[DoiVerdict]: ...


@dataclass
class _Ctx:
    session_maker: async_sessionmaker[AsyncSession]
    principal: Principal
    freshness_threshold: timedelta = timedelta(days=180)
    scholar: SupportsVerifyDois | None = None


# Held in a ContextVar, NOT a module global.
#
# arq runs jobs concurrently in a single event loop. With a global, job B
# overwrites job A's context, A's `finally` then clears it, and B's next node
# reads `None` — surfacing as "context not initialized" and failing the job.
# That is not hypothetical: it failed 11 of 14 papers in one research run,
# because a research run is exactly the concurrent case. A ContextVar is
# per-task, so each job sees only its own context.
_active_ctx_var: ContextVar[_Ctx | None] = ContextVar(
    "aleph_mechanical_review_active_ctx", default=None
)


def _ctx() -> _Ctx:
    ctx = _active_ctx_var.get()
    if ctx is None:
        msg = "MechanicalReviewerWorkflow context not initialized"
        raise RuntimeError(msg)
    return ctx


class _MechanicalReviewInput(TypedDict):
    project_id: UUID
    revision_id: UUID
    page_id: UUID
    agent_run_id: UUID
    review_run_id: UUID


class MechanicalReviewState(_MechanicalReviewInput, total=False):
    # Per-node finding counts — MUST be declared here: LangGraph silently
    # drops node updates to keys absent from the state schema.
    n_citation: int
    n_doi: int
    n_links: int
    n_stale: int
    n_dupe: int
    finding_count: int


@with_phase("citation_match", ctx_getter=lambda: _ctx())
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
                (await session.execute(select(WikiClaim).where(WikiClaim.revision_id == rev.id)))
                .scalars()
                .all()
            )
            claim_ids = [c.id for c in claim_rows]
            citation_rows = (
                list(
                    (
                        await session.execute(
                            select(Citation).where(Citation.claim_id.in_(claim_ids))
                        )
                    )
                    .scalars()
                    .all()
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


async def _registry_sources(session: AsyncSession, *, revision_id: UUID) -> list[Source]:
    """Sources registered on a revision via claim → citation → source page."""
    stmt = (
        select(Source)
        .join(SourcePage, SourcePage.source_id == Source.id)
        .join(Citation, Citation.source_page_id == SourcePage.id)
        .join(WikiClaim, WikiClaim.id == Citation.claim_id)
        .where(WikiClaim.revision_id == revision_id)
    )
    sources: list[Source] = []
    seen: set[UUID] = set()
    for src in (await session.execute(stmt)).scalars():
        if src.id not in seen:
            seen.add(src.id)
            sources.append(src)
    return sources


@with_phase("doi_verification", ctx_getter=lambda: _ctx())
async def _node_doi_verification(state: MechanicalReviewState) -> dict[str, int]:
    """Verify cited DOIs (spec WP-2 §6).

    Gathers DOIs from the revision body markdown and from the revision's
    source-registry rows (`source_metadata_jsonb["doi"]`), verifies them via
    the injected scholar service (≤ DOI_VERIFY_CAP per run), and emits:

    - ``fabricated_doi`` (high) when a verdict is authoritatively ``ok=False``
    - ``retracted_source`` (critical) when ``retracted=True``
    - nothing when ``ok=None`` — network-unverifiable is never flagged.

    Verdicts for source-row DOIs are cached back into that source's
    ``source_metadata_jsonb["doi_verdict"]`` in the same transaction as the
    findings, with a ``source.update`` ledger event. Upstream trouble
    (`ScholarUpstreamError`) never fails the review run — all unresolved
    DOIs are treated as ``ok=None``.
    """
    ctx = _ctx()
    if ctx.scholar is None:
        return {"n": 0}
    n = 0
    with start_span("review.mechanical.doi_verification"):
        async with ctx.session_maker() as session:
            rev = await session.get(WikiRevision, state["revision_id"])
            if rev is None:
                return {"n": 0}
            # (a) DOIs cited in the revision body.
            body_dois = extract_dois(rev.body_md)
            # (b) DOIs recorded on the revision's ingested source rows.
            source_by_doi: dict[str, Source] = {}
            for src in await _registry_sources(session, revision_id=rev.id):
                metadata: dict[str, Any] = src.source_metadata_jsonb
                raw = metadata.get("doi")
                if isinstance(raw, str) and raw.strip():
                    source_by_doi.setdefault(normalize_doi(raw), src)
            ordered: list[str] = []
            seen: set[str] = set()
            for doi in [*body_dois, *source_by_doi]:
                if doi not in seen:
                    seen.add(doi)
                    ordered.append(doi)
            if not ordered:
                return {"n": 0}
            skipped = max(0, len(ordered) - DOI_VERIFY_CAP)
            if skipped:
                _log.warning(
                    "review.mechanical.doi_verification.cap_exceeded",
                    revision_id=str(rev.id),
                    total=len(ordered),
                    cap=DOI_VERIFY_CAP,
                    skipped=skipped,
                )
            cap_note = (
                f" Note: {skipped} additional DOI(s) exceeded the {DOI_VERIFY_CAP}-per-run "
                "verification cap and were not checked."
                if skipped
                else ""
            )
            try:
                verdicts = await ctx.scholar.verify_dois(ordered[:DOI_VERIFY_CAP])
            except ScholarUpstreamError:
                # Upstream trouble — everything unresolved is ok=None: no findings.
                _log.warning(
                    "review.mechanical.doi_verification.upstream_unavailable",
                    revision_id=str(rev.id),
                )
                return {"n": 0}
            ledger = LedgerWriter(session)
            checked_at = utcnow().isoformat()
            for verdict in verdicts:
                src = source_by_doi.get(verdict.doi)
                evidence: list[dict[str, Any]] = [
                    {"kind": "doi", "value": verdict.doi},
                    {"kind": "checked_via", "value": verdict.checked_via},
                ]
                if verdict.ok is False:
                    await add_finding(
                        session,
                        review_run_id=state["review_run_id"],
                        project_id=state["project_id"],
                        finding_kind="fabricated_doi",
                        severity="high",
                        title=f"Fabricated DOI: {verdict.doi}",
                        description=(
                            f"DOI {verdict.doi} cited in revision {rev.id} of page "
                            f"{state['page_id']} does not resolve on Crossref or OpenAlex "
                            f"(checked via {verdict.checked_via}).{cap_note}"
                        ),
                        target_page_id=state["page_id"],
                        target_revision_id=rev.id,
                        target_source_id=src.id if src is not None else None,
                        evidence_refs=evidence,
                        auto_resolvable=False,
                        created_by=ctx.principal.user_id,
                    )
                    n += 1
                if verdict.retracted is True:
                    if src is not None:
                        # Scholar-detected retraction funnels through the shared
                        # `retract_source` service (WP-6 §4): it retracts the
                        # source, flags dependent claims, and emits the critical
                        # `retracted_source` finding — the same path as a manual
                        # retract. Same session/transaction (rule 4).
                        await retract_source(
                            session,
                            ledger,
                            ctx.principal,
                            source_id=src.id,
                            reason=(
                                f"Scholar-detected retraction of DOI {verdict.doi} "
                                f"(checked via {verdict.checked_via})"
                            ),
                        )
                    else:
                        # A retracted DOI cited in the body with no ingested
                        # Source row — nothing to retract, but still surface it.
                        await add_finding(
                            session,
                            review_run_id=state["review_run_id"],
                            project_id=state["project_id"],
                            finding_kind="retracted_source",
                            severity="critical",
                            title=f"Retracted source: DOI {verdict.doi}",
                            description=(
                                f"DOI {verdict.doi} cited in revision {rev.id} of page "
                                f"{state['page_id']} has been retracted "
                                f"(checked via {verdict.checked_via}).{cap_note}"
                            ),
                            target_page_id=state["page_id"],
                            target_revision_id=rev.id,
                            target_source_id=None,
                            evidence_refs=evidence,
                            auto_resolvable=False,
                            created_by=ctx.principal.user_id,
                        )
                    n += 1
                # Cache authoritative verdicts back onto the ingested source row
                # (ok=None never overwrites — unverifiable is not knowledge).
                if src is not None and verdict.ok is not None:
                    doi_verdict = {
                        "ok": verdict.ok,
                        "retracted": verdict.retracted,
                        "checked_via": verdict.checked_via,
                        "checked_at": checked_at,
                    }
                    # Reassign the full dict — JSONB doesn't track in-place mutation.
                    existing_metadata: dict[str, Any] = src.source_metadata_jsonb
                    src.source_metadata_jsonb = {
                        **existing_metadata,
                        "doi_verdict": doi_verdict,
                    }
                    await ledger.append(
                        project_id=state["project_id"],
                        actor_id=ctx.principal.user_id,
                        actor_kind=ctx.principal.actor_kind,
                        action_kind="source.update",
                        target_id=src.id,
                        target_kind="source",
                        payload={"doi": verdict.doi, "doi_verdict": doi_verdict},
                        trace_id=current_trace_id(),
                    )
            await session.commit()
    return {"n": n}


@with_phase("broken_links", ctx_getter=lambda: _ctx())
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
                    evidence_refs=[{"kind": "wikilink", "dst_title": link.dst_title}],
                    auto_resolvable=False,
                    created_by=ctx.principal.user_id,
                )
                n += 1
            await session.commit()
    return {"n": n}


@with_phase("stale_sources", ctx_getter=lambda: _ctx())
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
            for _claim, cite in rows:
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


@with_phase("duplicate_sources", ctx_getter=lambda: _ctx())
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
                    description=(f"{count} source assets share the same sha256. Consider merging."),
                    evidence_refs=[{"kind": "sha256", "value": sha, "count": int(count)}],
                    auto_resolvable=False,
                    created_by=ctx.principal.user_id,
                )
                n += 1
            await session.commit()
    return {"n": n}


@with_phase("finalize", ctx_getter=lambda: _ctx())
async def _node_finalize(state: MechanicalReviewState) -> dict[str, int]:
    n = (
        state.get("n_citation", 0)
        + state.get("n_doi", 0)
        + state.get("n_links", 0)
        + state.get("n_stale", 0)
        + state.get("n_dupe", 0)
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
        session_maker: async_sessionmaker[AsyncSession],
        principal: Principal,
        scholar: SupportsVerifyDois | None = None,
    ) -> None:
        self._ctx = _Ctx(session_maker=session_maker, principal=principal, scholar=scholar)
        graph: StateGraph = StateGraph(MechanicalReviewState)
        # Wrap each node to fan results into typed slots on state.
        graph.add_node("citation_match", _wrap(_node_citation_match, "n_citation"))
        graph.add_node("doi_verification", _wrap(_node_doi_verification, "n_doi"))
        graph.add_node("broken_links", _wrap(_node_broken_links, "n_links"))
        graph.add_node("stale_sources", _wrap(_node_stale_sources, "n_stale"))
        graph.add_node("duplicate_sources", _wrap(_node_duplicate_sources, "n_dupe"))
        graph.add_node("finalize", _node_finalize)
        graph.add_edge(START, "citation_match")
        graph.add_edge("citation_match", "doi_verification")
        graph.add_edge("doi_verification", "broken_links")
        graph.add_edge("broken_links", "stale_sources")
        graph.add_edge("stale_sources", "duplicate_sources")
        graph.add_edge("duplicate_sources", "finalize")
        graph.add_edge("finalize", END)
        self._compiled = graph.compile()

    async def run(
        self, *, project_id: UUID, revision_id: UUID, page_id: UUID, agent_run_id: UUID
    ) -> int:
        token = _active_ctx_var.set(self._ctx)
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
            _active_ctx_var.reset(token)


def _wrap(handler, slot: str):
    async def _wrapped(state):  # type: ignore[no-untyped-def]
        out = await handler(state)
        return {slot: out.get("n", 0)}

    return _wrapped
