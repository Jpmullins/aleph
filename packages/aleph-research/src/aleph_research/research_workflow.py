"""Native research loop (WP-3 §1).

LangGraph workflow, in-process in ``aleph-workers`` — no HTTP callbacks,
no polling, no slots:

    plan → search → ingest → reflect ─(loop ≤ max_iterations,
                                 plateau cutoff)→ search
                                  └──→ compose → synthesize → END

* **plan** (LLM, ``research.plan``): topic → 3-6 subqueries tagged with
  preferred tool kinds.
* **search**: fan the subqueries across the *bound* connectors plus the
  scholar path (``search_openalex`` / ``expand_citations``). Pure HTTP,
  no LLM. Results deduped by URL/DOI.
* **ingest** (LLM triage, ``research.triage``): select ≤
  ``max_sources_per_iter``, verify DOIs (``ok=False`` dropped, never
  ingested), fetch via each result's connector, register with
  ``register_uploaded_source`` and kick the normalize pipeline.
* **reflect** (LLM, ``research.reflect``): decide gaps → new subqueries,
  or done. Bounds: ``max_iterations``; plateau cutoff: an iteration that
  ingests 0 new sources stops the loop regardless.
* **compose** (LLM, ``research.compose``): retrieve an *evidence pack*
  from the chunks of the sources this run ingested — one marker per
  CHUNK, under a hard character budget (:mod:`aleph_research.evidence`)
  — write ``body_md`` citing those markers, require a verbatim quote per
  marker and ground it against the chunk it claims to come from,
  normalize with the LLM-free ``style_pass``, and build a
  :class:`ResearchReport` whose every citation carries a chunk id and a
  character span.

  This node used to build the model's entire evidence context as
  ``"\n".join(f"c{i}: {title} — {url}")``. Titles and links: no source
  text reached the model at any point, so the report was written from
  the model's own recollection and the citation numbers were assigned by
  position in a list. That is the defect ``WS-RS7`` exists to remove.
* **synthesize**: hand the report to the unchanged ``SynthesisWorkflow``
  and enqueue ``curate_page_job`` per committed page.

This module mirrors the ``SynthesisWorkflow`` structure so the same
ContextVar-based ``_active_ctx`` pattern works; every node is decorated
with ``@with_phase`` so Activity streams progress automatically.
"""

# pyright: reportMissingTypeStubs=false
# (first-party aleph_* packages ship inline types but no py.typed marker —
#  the same visible debt every other package in this repo carries)

from __future__ import annotations

import asyncio
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

# UUID must be importable at runtime: LangGraph's StateGraph resolves the
# state TypedDict's annotations with get_type_hints() at graph construction.
from uuid import UUID

import structlog
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from aleph_connectors.base import ConnectorContext, ConnectorResult, SearchQuery
from aleph_core.ids import uuid7
from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.repos.agent_events import with_phase
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.client import ChatMessage
from aleph_observability.tracing import start_span
from aleph_research.evidence import (
    EVIDENCE_CHAR_BUDGET,
    ChunkRef,
    EvidenceCard,
    anchor_body,
    extract_claims,
    render_pack,
    renumber_markers,
    select_cards,
    split_evidence_block,
)
from aleph_rks.models import DocumentChunk, Source
from aleph_rks.retrieval import descend_into_source, search_corpus
from aleph_rks.source_service import mark_status, register_uploaded_source
from aleph_scholar.dois import normalize_doi
from aleph_security.agent_token import mint_agent_token
from aleph_wiki.synthesis_workflow import (
    ResearchClaim,
    ResearchReport,
    ResearchSourceRef,
    SynthesisState,
    SynthesisWorkflow,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_models.client import LiteLLMClient
    from aleph_research.tools import BoundTool
    from aleph_rks.asset_store import AssetStore
    from aleph_rks.source_service import SourceCreated
    from aleph_scholar import ScholarService
    from aleph_scholar.types import DoiVerdict, WorkRef
    from aleph_security.principal import Principal

_log = structlog.get_logger(__name__)

#: Hard cap on a single connector fetch (attacker-influenceable bytes).
MAX_FETCH_BYTES = 10 * 1024 * 1024

_SEARCH_RESULTS_PER_TOOL = 5
_EXPANSION_SEED_SOURCES = 2
_EXPANSION_LIMIT = 5

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_CITE_MARKER_RE = re.compile(r"\[c(\d+)\]")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchLimits:
    max_iterations: int = 3
    max_sources_per_iter: int = 6
    max_total_sources: int = 15
    #: How long compose waits for the sources this run ingested to become
    #: retrievable. Ingest ENQUEUES `normalize_job`; normalize enqueues
    #: `chunk_embed`. Both run in another worker task, so at the moment the
    #: graph reaches compose the chunks of everything it just downloaded
    #: usually do not exist yet — and an evidence pack built a second too
    #: early is empty, which reads exactly like "these documents say nothing".
    #: Bounded because arq's `job_timeout` for this worker is 600s and the
    #: loop has already spent several LLM calls by here.
    index_wait_seconds: float = 90.0
    index_poll_seconds: float = 2.0


@dataclass(frozen=True)
class Subquery:
    query: str
    tools: tuple[str, ...] = ()
    """Preferred connector kinds; empty means every bound tool."""


@dataclass(frozen=True)
class Candidate:
    kind: str
    external_id: str
    title: str
    url: str | None
    snippet: str | None
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class IngestedSource:
    short_id: str
    #: Required, not optional. Compose retrieves the run's own chunks by
    #: `document_chunks.source_id`, so a source this loop cannot name by id is
    #: a source it cannot read — and a `None` here would degrade that to an
    #: empty pack rather than a type error.
    source_id: UUID
    title: str
    url: str | None
    kind: str
    snippet: str | None = None
    doi: str | None = None
    openalex_id: str | None = None


# ---------------------------------------------------------------------------
# Context (ContextVar, not a module global — concurrent runs in one worker
# process must not clobber each other)
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    session_maker: async_sessionmaker[AsyncSession]
    litellm: LiteLLMClient
    principal: Principal
    scholar: ScholarService
    asset_store: AssetStore
    tools_by_kind: dict[str, BoundTool]
    profile_bindings: dict[str, Any]
    limits: ResearchLimits
    agent_token_secret: str
    enqueue: Callable[..., Awaitable[object]]
    """Arq-style ``enqueue_job(name, *args)`` for normalize/curate kicks."""


_active_ctx_var: ContextVar[_Ctx | None] = ContextVar("aleph_research_active_ctx", default=None)


def _ctx() -> _Ctx:
    ctx = _active_ctx_var.get()
    if ctx is None:
        msg = "ResearchWorkflow context not initialized"
        raise RuntimeError(msg)
    return ctx


class ResearchState(TypedDict):
    agent_run_id: UUID | None
    project_id: UUID
    topic: str
    depth: NotRequired[str]
    subqueries: NotRequired[list[Subquery]]
    #: Every question this run has asked — the plan's, plus reflect's gap
    #: queries. `subqueries` is overwritten each iteration with the CURRENT
    #: batch, so by compose time it holds only the last reflect's gaps; the
    #: evidence pack has to retrieve against everything the run set out to
    #: answer, not against whatever it asked most recently.
    plan_subqueries: NotRequired[list[Subquery]]
    iteration: NotRequired[int]
    candidates: NotRequired[list[Candidate]]
    seen_keys: NotRequired[list[str]]
    ingested: NotRequired[list[IngestedSource]]
    last_ingested_count: NotRequired[int]
    research_done: NotRequired[bool]
    report: NotRequired[ResearchReport]
    committed_revision_ids: NotRequired[list[UUID]]
    committed_page_ids: NotRequired[list[UUID]]
    proposal_ids: NotRequired[list[UUID]]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without DB or network)
# ---------------------------------------------------------------------------


def _loads_lenient(text: str) -> dict[str, Any]:
    """Recover a JSON object from an LLM response.

    The gateway does not reliably honor ``response_format``: the model may
    wrap JSON in ``` fences or prefix it with prose. Try a direct parse,
    then the first ``{...}`` span. Returns ``{}`` if nothing parses.
    """
    s = (text or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        return obj  # pyright: ignore[reportUnknownVariableType]
    m = _JSON_OBJ_RE.search(s)
    if m is None:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def parse_plan(text: str, *, topic: str, bound_kinds: Sequence[str]) -> list[Subquery]:
    """Parse the plan response; malformed JSON degrades to one topic-wide subquery."""
    parsed = _loads_lenient(text)
    subs: list[Subquery] = []
    raw = parsed.get("subqueries")
    if isinstance(raw, list):
        for item in raw[:6]:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, dict):
                query = str(item.get("query") or "").strip()  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                tools_raw = item.get("tools")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
                tools: tuple[str, ...] = ()
                if isinstance(tools_raw, list):
                    tools = tuple(
                        str(t)
                        for t in tools_raw  # pyright: ignore[reportUnknownVariableType]
                        if isinstance(t, str) and t in bound_kinds
                    )
                if query:
                    subs.append(Subquery(query=query, tools=tools))
            elif isinstance(item, str) and item.strip():
                subs.append(Subquery(query=item.strip()))
    if not subs:
        subs = [Subquery(query=topic)]
    return subs


def parse_reflect(text: str) -> tuple[bool, list[str]]:
    """Parse the reflect response → (done, gap subqueries). Malformed → done."""
    parsed = _loads_lenient(text)
    if not parsed:
        return (True, [])
    done = bool(parsed.get("done", False))
    gaps_raw = parsed.get("gaps")
    gaps: list[str] = []
    if isinstance(gaps_raw, list):
        gaps = [str(g).strip() for g in gaps_raw if str(g).strip()]  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
    if done or not gaps:
        return (True, [])
    return (False, gaps[:6])


def parse_triage(text: str, *, n_candidates: int, max_select: int) -> list[int]:
    """Parse the triage response → selected candidate indices.

    A well-formed ``selected`` list is honored even when empty (the model
    judged nothing relevant); malformed JSON falls back to the first
    ``max_select`` candidates so a flaky response never zeroes an iteration.
    """
    parsed = _loads_lenient(text)
    raw = parsed.get("selected")
    if isinstance(raw, list):
        picks: list[int] = []
        for v in raw:  # pyright: ignore[reportUnknownVariableType]
            try:
                i = int(v)  # pyright: ignore[reportUnknownArgumentType]
            except (TypeError, ValueError):
                continue
            if 0 <= i < n_candidates and i not in picks:
                picks.append(i)
        return picks[:max_select]
    return list(range(min(n_candidates, max_select)))


def should_stop(
    *,
    iteration: int,
    last_ingested_count: int,
    total_ingested: int,
    limits: ResearchLimits,
) -> bool:
    """Loop cutoff: plateau (0 new sources), iteration bound, or total-source cap."""
    if last_ingested_count <= 0:
        return True
    if iteration + 1 >= limits.max_iterations:
        return True
    return total_ingested >= limits.max_total_sources


def sanitize_markers(body_md: str, n_sources: int) -> str:
    """Drop ``[cN]`` markers pointing past the real source list.

    Citation verification blocks the commit on unknown markers; a
    hallucinated ``[c9]`` against 5 sources must not fail the whole run.
    """

    def _sub(m: re.Match[str]) -> str:
        k = int(m.group(1))
        return m.group(0) if 1 <= k <= n_sources else ""

    return _CITE_MARKER_RE.sub(_sub, body_md)


def summarize_body(body_md: str, *, fallback: str) -> str:
    """First non-heading paragraph, flattened and trimmed."""
    for block in body_md.split("\n\n"):
        t = block.strip()
        if t and not t.startswith("#"):
            return t.replace("\n", " ")[:2048]
    return fallback[:2048]


def build_report(
    *,
    topic: str,
    body_md: str,
    summary: str,
    refs_by_marker: dict[str, ResearchSourceRef],
    claims: Sequence[ResearchClaim],
) -> ResearchReport:
    """Assemble the report from ALREADY-ANCHORED citations.

    Two things changed here, and both were defects.

    ``refs_by_marker`` used to be built as ``{f"c{i}": ingested[i]}`` — the
    marker was a position in the download list, so ``[c3]`` asserted nothing
    about what any document said. It is now keyed by the chunk a grounded quote
    came from.

    ``claims`` used to be empty, unconditionally. The loop wrote citation
    markers into prose and then discarded the statements they were attached to,
    so ``_node_commit_revision`` iterated an empty list and the research path
    wrote no claims and no citations at all. Measured on the live stack before
    this change: seven succeeded research runs, seven synthesis proposals, zero
    citations on any synthesis page.

    ``sources`` is the distinct sources actually cited, in marker order — one
    ref per source, carrying that source's first anchored span.
    """
    seen: set[str] = set()
    sources: list[ResearchSourceRef] = []
    for marker in sorted(refs_by_marker, key=_marker_ordinal):
        ref = refs_by_marker[marker]
        if ref.source_short_id in seen:
            continue
        seen.add(ref.source_short_id)
        sources.append(ref)
    return ResearchReport(
        topic=topic,
        body_md=body_md,
        summary=summary,
        sources=sources,
        citations_by_marker=dict(refs_by_marker),
        claims=list(claims),
    )


def _marker_ordinal(marker: str) -> int:
    return int(marker[1:]) if marker[1:].isdigit() else 0


def dedup_key(candidate: Candidate) -> str:
    doi_raw = candidate.metadata.get("doi")
    if doi_raw:
        doi = normalize_doi(str(doi_raw))
        if doi:
            return f"doi:{doi}"
    if candidate.url:
        return f"url:{candidate.url}"
    return f"{candidate.kind}:{candidate.external_id}"


def apply_doi_verdicts(
    candidates: Sequence[Candidate],
    verdicts: Sequence[DoiVerdict],
) -> list[tuple[Candidate, DoiVerdict | None]]:
    """Pair candidates with their DOI verdicts; ``ok=False`` results are dropped.

    ``ok=None`` (network-unverifiable) and DOI-less candidates pass through —
    only an authoritative double-404 excludes a result from ingest.
    """
    by_doi = {v.doi: v for v in verdicts}
    out: list[tuple[Candidate, DoiVerdict | None]] = []
    for cand in candidates:
        doi_raw = cand.metadata.get("doi")
        verdict = by_doi.get(normalize_doi(str(doi_raw))) if doi_raw else None
        if verdict is not None and verdict.ok is False:
            continue
        out.append((cand, verdict))
    return out


def _candidate_from_work(work: WorkRef, *, query: str) -> Candidate | None:
    url = work.pdf_url or work.landing_url or (f"https://doi.org/{work.doi}" if work.doi else None)
    if not url:
        return None  # nothing fetchable
    return Candidate(
        kind="openalex",
        external_id=work.openalex_id or work.doi or url,
        title=work.title,
        url=url,
        snippet=None,
        metadata={
            "doi": work.doi,
            "openalex_id": work.openalex_id,
            "publication_year": work.year,
            "query": query,
        },
    )


def _safe_filename(candidate: Candidate, extension: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate.external_id).strip("._") or "source"
    ext = (extension or "bin").lstrip(".")[:16]
    return f"{base[:120]}.{ext}"


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_PLAN_SYS = (
    "You are planning background research to grow a project wiki. Given a "
    "research topic, return ONLY a raw JSON object (no prose, no markdown "
    'fences) of the form {"subqueries": [{"query": "...", "tools": '
    '["kind", ...]}]} with 3-6 distinct, specific subqueries that together '
    "cover the topic. Each subquery may name preferred tool kinds from the "
    'available set: {tools}. Use an empty "tools" list to search every '
    "available tool."
)

_TRIAGE_SYS = (
    "You triage search results for a research run. Return ONLY a raw JSON "
    'object {"selected": [i, ...]} — the zero-based indices of at most '
    "{max_n} results most relevant to the topic, best first. Select only "
    "genuinely relevant, substantive results; select nothing if none are."
)

_REFLECT_SYS = (
    "You review the coverage of an in-progress research run. Return ONLY a "
    'raw JSON object {"done": true|false, "gaps": ["subquery", ...]}. Set '
    '"done" to true when the collected sources adequately cover the topic; '
    "otherwise list at most 4 new, specific subqueries targeting what is "
    "missing (do not repeat ground already covered)."
)

#: The composer is given passages, not a bibliography, and it has to prove it
#: read them. The quote block is the proof: `anchor_body` grounds every quote
#: against the chunk it names, and a quote that is not there fails the run. The
#: previous prompt asked for markers "exactly matching the numbered source list"
#: — a list of titles and URLs — which is a request the model can satisfy
#: without having read anything at all.
_COMPOSE_SYS = (
    "You write a well-structured research report in Markdown for a project "
    "wiki, using ONLY the evidence cards you are given. Each card is a "
    "verbatim passage from a source, labelled [cN].\n\n"
    "Rules:\n"
    "1. Cite with the card labels: [c1], [c3]. Never invent a label.\n"
    "2. Every substantive sentence carries the [cN] of the card supporting "
    "it. Write nothing the cards do not support.\n"
    "3. Use ## section headings. Wrap key concepts in [[wikilinks]] when they "
    "merit their own wiki page. Do not add a references section — the wiki "
    "renders citations from the markers.\n"
    "4. After the body, and nothing after it, emit exactly one block:\n\n"
    "<!--aleph:evidence\n"
    '{"quotes": {"c1": "...", "c3": "..."}}\n'
    "-->\n\n"
    "For every [cN] you cited, the value is one or two sentences COPIED "
    "CHARACTER FOR CHARACTER from card cN. Not a paraphrase, not a summary, "
    "not a sentence you wrote. A quote that is not present in its card fails "
    "the run, and a marker you cite without quoting is deleted from the report."
)


async def _chat(
    state: ResearchState,
    *,
    capability: Capability,
    purpose: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> str:
    ctx = _ctx()
    resp = await ctx.litellm.chat(
        principal=ctx.principal,
        project_id=state["project_id"],
        agent_run_id=state["agent_run_id"],
        capability=capability,
        profile_bindings=ctx.profile_bindings,
        messages=[
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ],
        response_format={"type": "json_object"} if json_mode else None,
        max_tokens=max_tokens,
        temperature=temperature,
        purpose=purpose,
    )
    return (resp.choices[0].message.content if resp.choices else "") or ""


def _connector_ctx(state: ResearchState, tool: BoundTool) -> ConnectorContext:
    ctx = _ctx()
    return ConnectorContext(
        project_id=state["project_id"],
        user_id=ctx.principal.user_id,
        agent_run_id=state["agent_run_id"],
        correlation_id=ctx.principal.correlation_id,
        credential_value=tool.credential,
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@with_phase("plan", ctx_getter=lambda: _ctx())
async def _node_plan(state: ResearchState) -> dict[str, Any]:
    with start_span(
        "research.node.plan",
        **{"aleph.node": "plan", "aleph.project_id": str(state["project_id"])},
    ):
        ctx = _ctx()
        bound_kinds = sorted(ctx.tools_by_kind)
        content = await _chat(
            state,
            capability=Capability.SYNTHESIS,
            purpose="research.plan",
            system=_PLAN_SYS.replace("{tools}", ", ".join(bound_kinds) or "(none)"),
            user=f"Research topic: {state['topic']}",
            max_tokens=1200,
            json_mode=True,
        )
        subqueries = parse_plan(content, topic=state["topic"], bound_kinds=bound_kinds)
        return {
            "subqueries": subqueries,
            # Kept separately because `subqueries` is overwritten by every
            # reflect; compose retrieves against every question the run asked.
            "plan_subqueries": subqueries,
            "iteration": state.get("iteration", 0),
        }


@with_phase("search", ctx_getter=lambda: _ctx())
async def _node_search(state: ResearchState) -> dict[str, Any]:
    """Fan subqueries across the bound tools + the scholar path. No LLM."""
    with start_span(
        "research.node.search",
        **{"aleph.node": "search", "aleph.project_id": str(state["project_id"])},
    ):
        ctx = _ctx()
        subqueries = state.get("subqueries") or [Subquery(query=state["topic"])]
        seen: set[str] = set(state.get("seen_keys") or [])
        candidates: list[Candidate] = []

        def _add(c: Candidate) -> None:
            key = dedup_key(c)
            if key in seen:
                return
            seen.add(key)
            candidates.append(c)

        for sub in subqueries:
            kinds = [k for k in sub.tools if k in ctx.tools_by_kind] or sorted(ctx.tools_by_kind)
            for kind in kinds:
                tool = ctx.tools_by_kind[kind]
                try:
                    results = await tool.connector.search(
                        _connector_ctx(state, tool),
                        SearchQuery(text=sub.query, max_results=_SEARCH_RESULTS_PER_TOOL),
                    )
                except Exception as exc:
                    _log.warning(
                        "research.search_failed",
                        kind=kind,
                        query=sub.query[:120],
                        error=str(exc)[:200],
                    )
                    continue
                for r in results:
                    _add(
                        Candidate(
                            kind=kind,
                            external_id=r.external_id,
                            title=r.title,
                            url=r.url,
                            snippet=r.snippet,
                            metadata=dict(r.metadata or {}),
                        )
                    )
            # Scholar discovery path (OpenAlex is keyless but still gated on
            # the project's binding: no bound openalex kind → no scholar path).
            if "openalex" in ctx.tools_by_kind:
                try:
                    works = await ctx.scholar.search_openalex(
                        sub.query, per_page=_SEARCH_RESULTS_PER_TOOL
                    )
                except Exception as exc:
                    _log.warning(
                        "research.scholar_search_failed",
                        query=sub.query[:120],
                        error=str(exc)[:200],
                    )
                    works = []
                for work in works:
                    cand = _candidate_from_work(work, query=sub.query)
                    if cand is not None:
                        _add(cand)

        # Citation-graph expansion around what previous iterations landed.
        if state.get("iteration", 0) > 0 and "openalex" in ctx.tools_by_kind:
            seeds = [
                s.openalex_id or s.doi
                for s in reversed(state.get("ingested") or [])
                if s.openalex_id or s.doi
            ][:_EXPANSION_SEED_SOURCES]
            for ref in seeds:
                if ref is None:
                    continue
                try:
                    expansion = await ctx.scholar.expand_citations(ref, limit=_EXPANSION_LIMIT)
                except Exception as exc:
                    _log.warning("research.expand_failed", ref=ref[:120], error=str(exc)[:200])
                    continue
                for work in [*expansion.backward, *expansion.forward]:
                    cand = _candidate_from_work(work, query=f"citations of {ref}")
                    if cand is not None:
                        _add(cand)

        return {"candidates": candidates, "seen_keys": sorted(seen)}


async def _kick_normalize(
    session: AsyncSession,
    *,
    ledger: LedgerWriter,
    project_id: UUID,
    created: SourceCreated,
) -> tuple[str, str]:
    """Ledger a normalizer AgentRun + mint its token (the sources-route pattern).

    Returns ``(source_version_id, agent_token)`` for the post-commit enqueue.
    """
    ctx = _ctx()
    run_id = uuid7()
    correlation_id = f"normalize-{run_id.hex}"
    session.add(
        AgentRun(
            id=run_id,
            project_id=project_id,
            agent_kind="normalizer",
            correlation_id=correlation_id,
            status="pending",
            input_payload={
                "source_id": str(created.source.id),
                "source_version_id": str(created.version.id),
            },
            created_by=ctx.principal.user_id,
        )
    )
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=ctx.principal.user_id,
        actor_kind=ctx.principal.actor_kind,
        action_kind="agent_run.create",
        target_id=run_id,
        target_kind="agent_run",
        payload={
            "agent_kind": "normalizer",
            "correlation_id": correlation_id,
            "source_id": str(created.source.id),
        },
        trace_id=None,
    )
    token = mint_agent_token(
        secret=ctx.agent_token_secret,
        user_id=ctx.principal.user_id,
        project_id=project_id,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )
    return (str(created.version.id), token)


async def _mark_source_failed(project_id: UUID, source_id: UUID, reason: str) -> None:
    ctx = _ctx()
    async with ctx.session_maker() as session:
        await mark_status(
            session,
            ledger=LedgerWriter(session),
            principal=ctx.principal,
            project_id=project_id,
            source_id=source_id,
            status="failed",
            failure_reason=reason[:2048],
        )
        await session.commit()


@with_phase("ingest", ctx_getter=lambda: _ctx())
async def _node_ingest(state: ResearchState) -> dict[str, Any]:
    with start_span(
        "research.node.ingest",
        **{"aleph.node": "ingest", "aleph.project_id": str(state["project_id"])},
    ):
        ctx = _ctx()
        candidates = state.get("candidates") or []
        ingested = list(state.get("ingested") or [])
        remaining = ctx.limits.max_total_sources - len(ingested)
        if not candidates or remaining <= 0:
            return {"last_ingested_count": 0, "candidates": []}
        max_n = min(ctx.limits.max_sources_per_iter, remaining)

        listing = "\n".join(
            f"{i}. [{c.kind}] {c.title} — {c.url or c.external_id}"
            + (f" :: {c.snippet[:200]}" if c.snippet else "")
            for i, c in enumerate(candidates)
        )
        content = await _chat(
            state,
            capability=Capability.CLASSIFICATION,
            purpose="research.triage",
            system=_TRIAGE_SYS.replace("{max_n}", str(max_n)),
            user=f"Topic: {state['topic']}\n\nResults:\n{listing}",
            max_tokens=400,
            json_mode=True,
        )
        picks = parse_triage(content, n_candidates=len(candidates), max_select=max_n)
        selected = [candidates[i] for i in picks]

        # DOI verification for any selected result carrying a DOI. ok=False
        # results are dropped, never ingested.
        dois = sorted(
            {normalize_doi(str(c.metadata.get("doi"))) for c in selected if c.metadata.get("doi")}
            - {""}
        )
        verdicts: list[DoiVerdict] = []
        if dois:
            try:
                verdicts = list(await ctx.scholar.verify_dois(dois))
            except Exception as exc:
                _log.warning("research.doi_verify_failed", error=str(exc)[:200])

        new_entries: list[IngestedSource] = []
        for cand, verdict in apply_doi_verdicts(selected, verdicts):
            tool = ctx.tools_by_kind.get(cand.kind)
            if tool is None:
                continue
            try:
                payload = await tool.connector.fetch(
                    _connector_ctx(state, tool),
                    ConnectorResult(
                        external_id=cand.external_id,
                        title=cand.title,
                        url=cand.url,
                        snippet=cand.snippet,
                        metadata=cand.metadata,
                    ),
                )
            except Exception as exc:
                _log.warning(
                    "research.fetch_failed",
                    kind=cand.kind,
                    url=cand.url,
                    error=str(exc)[:200],
                )
                continue
            if len(payload.data) > MAX_FETCH_BYTES:
                _log.warning(
                    "research.fetch_too_large",
                    kind=cand.kind,
                    url=cand.url,
                    size_bytes=len(payload.data),
                )
                continue

            meta: dict[str, Any] = dict(cand.metadata)
            if verdict is not None:
                meta["doi"] = verdict.doi
                if verdict.openalex_id:
                    meta["openalex_id"] = verdict.openalex_id
                meta["doi_verdict"] = {
                    "ok": verdict.ok,
                    "retracted": verdict.retracted,
                    "checked_via": verdict.checked_via,
                    "checked_at": utcnow().isoformat(),
                }

            async with ctx.session_maker() as session:
                ledger = LedgerWriter(session)
                created = await register_uploaded_source(
                    session,
                    ledger=ledger,
                    principal=ctx.principal,
                    asset_store=ctx.asset_store,
                    project_id=state["project_id"],
                    title=cand.title[:512],
                    data=payload.data,
                    filename=_safe_filename(cand, payload.extension),
                    mime_type=payload.mime_type,
                    connector_kind=cand.kind,
                    source_metadata=meta,
                )
                version_id, norm_token = await _kick_normalize(
                    session,
                    ledger=ledger,
                    project_id=state["project_id"],
                    created=created,
                )
                short_id = created.source.short_id
                title = created.source.title
                source_id = created.source.id
                await session.commit()
            try:
                await ctx.enqueue("normalize_job", version_id, norm_token)
            except Exception as exc:
                await _mark_source_failed(
                    state["project_id"],
                    source_id,
                    f"failed to enqueue normalize job: {exc}",
                )
            new_entries.append(
                IngestedSource(
                    short_id=short_id,
                    source_id=source_id,
                    title=title,
                    url=cand.url,
                    kind=cand.kind,
                    snippet=cand.snippet,
                    doi=meta.get("doi"),
                    openalex_id=meta.get("openalex_id"),
                )
            )

        return {
            "ingested": ingested + new_entries,
            "last_ingested_count": len(new_entries),
            "candidates": [],
        }


@with_phase("reflect", ctx_getter=lambda: _ctx())
async def _node_reflect(state: ResearchState) -> dict[str, Any]:
    with start_span(
        "research.node.reflect",
        **{"aleph.node": "reflect", "aleph.project_id": str(state["project_id"])},
    ):
        ctx = _ctx()
        iteration = state.get("iteration", 0)
        ingested = state.get("ingested") or []
        if should_stop(
            iteration=iteration,
            last_ingested_count=state.get("last_ingested_count", 0),
            total_ingested=len(ingested),
            limits=ctx.limits,
        ):
            return {"research_done": True}
        listing = "\n".join(
            f"- [{s.kind}] {s.title}" + (f" :: {s.snippet[:160]}" if s.snippet else "")
            for s in ingested
        )
        content = await _chat(
            state,
            capability=Capability.SYNTHESIS,
            purpose="research.reflect",
            system=_REFLECT_SYS,
            user=f"Topic: {state['topic']}\n\nCollected sources:\n{listing}",
            max_tokens=600,
            json_mode=True,
        )
        done, gaps = parse_reflect(content)
        if done:
            return {"research_done": True}
        gap_subqueries = [Subquery(query=g) for g in gaps]
        return {
            "research_done": False,
            "subqueries": gap_subqueries,
            "plan_subqueries": (state.get("plan_subqueries") or []) + gap_subqueries,
            "iteration": iteration + 1,
        }


def route_after_reflect(state: ResearchState) -> str:
    return "compose" if state.get("research_done", True) else "search"


# ---------------------------------------------------------------------------
# Evidence retrieval — the half of the evidence pack that needs a database and
# a gateway. The selection policy and the grounding are pure and live in
# `aleph_research.evidence`.
# ---------------------------------------------------------------------------

#: Questions the evidence pack retrieves against: the topic, then the run's
#: sub-questions. Capped because each one costs a `search_corpus` round trip
#: plus a share of the character budget, and past ~6 the round-robin gives
#: every question one card and nothing gets depth.
_MAX_EVIDENCE_QUERIES = 6

#: Over-fetch per question. `search_corpus` has no source filter (that is
#: `aleph_rks.retrieval`, which this workstream does not own), so hits from
#: sources this run did not ingest are dropped afterwards — in a project with a
#: pre-existing corpus that can be most of them.
_CORPUS_TOP_K = 18
_CORPUS_PER_SOURCE_CAP = 3

#: Depth: how many of the strongest sources get a second, source-pinned pass,
#: and how much of each. `descend_into_source` IS scoped, so these hits never
#: need filtering.
_DESCEND_TOP_SOURCES = 3
_DESCENT_TOP_K = 3


async def _await_chunks(state: ResearchState, source_ids: set[UUID]) -> dict[UUID, int]:
    """Block until this run's sources are retrievable, or the deadline passes.

    Ingest ENQUEUES `normalize_job`, which enqueues `chunk_embed`; both run in
    another worker task. Without this barrier compose reaches the corpus
    milliseconds after the download and finds nothing — and an empty evidence
    pack is indistinguishable, from inside compose, from "these documents are
    about something else".

    Returns chunk counts per source. A source that never appears is named in
    the log: a research run that quietly composed from two of the six documents
    it downloaded is the failure this whole workstream is about.

    Deliberately not reaped or retried here — `reap_stale_runs` owns dead index
    jobs. This only waits, and only for as long as `limits.index_wait_seconds`.
    """
    ctx = _ctx()
    if not source_ids:
        return {}
    deadline = monotonic() + ctx.limits.index_wait_seconds
    counts: dict[UUID, int] = {}
    while True:
        async with ctx.session_maker() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk.source_id, func.count())
                    .where(DocumentChunk.source_id.in_(source_ids))
                    .group_by(DocumentChunk.source_id)
                )
            ).all()
            counts = {row[0]: int(row[1]) for row in rows}
            waiting = source_ids - set(counts)
            if waiting:
                # A source whose ingest FAILED will never produce a chunk.
                # Waiting the full deadline for it delays every run that hits
                # one bad PDF by the whole budget.
                dead = set(
                    (
                        await session.execute(
                            select(Source.id).where(
                                Source.id.in_(waiting), Source.status == "failed"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                waiting -= dead
        if not waiting or monotonic() >= deadline:
            if waiting:
                _log.warning(
                    "research.evidence.sources_not_indexed",
                    project_id=str(state["project_id"]),
                    waited_seconds=round(ctx.limits.index_wait_seconds, 1),
                    not_indexed=[str(sid) for sid in sorted(waiting, key=str)],
                    indexed=len(counts),
                )
            return counts
        await asyncio.sleep(ctx.limits.index_poll_seconds)


def _evidence_queries(state: ResearchState) -> list[str]:
    """The topic first, then every distinct sub-question the run asked."""
    queries: list[str] = [state["topic"].strip()]
    for sub in state.get("plan_subqueries") or state.get("subqueries") or []:
        q = sub.query.strip()
        if q and q not in queries:
            queries.append(q)
    # An empty query text would reach `or_tsquery` as an empty tsquery, which
    # matches nothing while looking like a search that ran.
    return [q for q in queries[:_MAX_EVIDENCE_QUERIES] if q] or [state["topic"]]


async def _embed_queries(state: ResearchState, queries: Sequence[str]) -> list[list[float] | None]:
    """Embed the questions, or degrade to lexical-only OUT LOUD.

    `search_corpus` reads `None` as "run the lexical leg only". Handing it a
    zero vector instead looks equivalent and is not: cosine distance to the
    zero vector is degenerate, so the dense leg returns an arbitrary page of
    rows and RRF fuses that noise as though it were a ranking.
    """
    ctx = _ctx()
    try:
        result = await ctx.litellm.embed(
            principal=ctx.principal,
            project_id=state["project_id"],
            agent_run_id=state["agent_run_id"],
            profile_bindings=ctx.profile_bindings,
            input=list(queries),
            purpose="research.evidence.query_embed",
        )
    except Exception as exc:
        _log.warning(
            "research.evidence.embed_failed",
            project_id=str(state["project_id"]),
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return [None] * len(queries)
    vectors = list(result.embeddings)
    if len(vectors) != len(queries):
        _log.warning(
            "research.evidence.embed_count_mismatch",
            expected=len(queries),
            got=len(vectors),
        )
        return [None] * len(queries)
    return list(vectors)


async def _chunk_refs(
    session: AsyncSession,
    *,
    hits: Sequence[Any],
    by_source: dict[UUID, IngestedSource],
) -> dict[UUID, ChunkRef]:
    """Turn `ChunkHit`s into citable refs by reading each chunk's document span.

    `ChunkHit` carries text and a score but not `char_start`/`char_end`, and
    those are what make a marker resolve to an exact span of the document. They
    are read here rather than added to `ChunkHit` because `aleph_rks.retrieval`
    belongs to another workstream.
    """
    chunk_ids = {h.chunk_id for h in hits}
    if not chunk_ids:
        return {}
    rows = (
        await session.execute(
            select(
                DocumentChunk.id,
                DocumentChunk.source_id,
                DocumentChunk.text,
                DocumentChunk.section_path,
                DocumentChunk.char_start,
                DocumentChunk.char_end,
            ).where(DocumentChunk.id.in_(chunk_ids))
        )
    ).all()
    refs: dict[UUID, ChunkRef] = {}
    for row in rows:
        source = by_source.get(row.source_id)
        if source is None:
            continue
        refs[row.id] = ChunkRef(
            chunk_id=row.id,
            source_id=row.source_id,
            source_short_id=source.short_id,
            title=source.title,
            url=source.url,
            section_path=row.section_path,
            text=row.text,
            char_start=row.char_start,
            char_end=row.char_end,
        )
    return refs


async def _gather_evidence(
    state: ResearchState, ingested: Sequence[IngestedSource]
) -> list[EvidenceCard]:
    """Retrieve the pack: breadth across the run's questions, then depth."""
    ctx = _ctx()
    by_source = {s.source_id: s for s in ingested}
    await _await_chunks(state, set(by_source))

    queries = _evidence_queries(state)
    vectors = await _embed_queries(state, queries)

    async with ctx.session_maker() as session:
        breadth_hits: list[list[Any]] = []
        for query, vector in zip(queries, vectors, strict=True):
            hits = await search_corpus(
                session,
                project_id=state["project_id"],
                query_text=query,
                query_embedding=vector,
                top_k=_CORPUS_TOP_K,
                per_source_cap=_CORPUS_PER_SOURCE_CAP,
            )
            breadth_hits.append([h for h in hits if h.source_id in by_source])

        # Depth into the sources the breadth pass ranked highest. If breadth
        # returned nothing at all — a lexical-only leg against a question whose
        # words appear in no chunk — fall back to descending into the run's own
        # sources with the topic, so a pack is still built from what was read.
        ranked: list[UUID] = []
        for rank in range(_CORPUS_TOP_K):
            for lst in breadth_hits:
                if rank < len(lst) and lst[rank].source_id not in ranked:
                    ranked.append(lst[rank].source_id)
        if not ranked:
            ranked = list(by_source)
        depth_hits: list[Any] = []
        for source_id in ranked[:_DESCEND_TOP_SOURCES]:
            depth_hits.extend(
                await descend_into_source(
                    session,
                    project_id=state["project_id"],
                    source_id=source_id,
                    query_text=queries[0],
                    query_embedding=vectors[0],
                    top_k=_DESCENT_TOP_K,
                )
            )

        refs = await _chunk_refs(
            session,
            hits=[h for lst in breadth_hits for h in lst] + depth_hits,
            by_source=by_source,
        )

    breadth = [[refs[h.chunk_id] for h in lst if h.chunk_id in refs] for lst in breadth_hits]
    depth = [refs[h.chunk_id] for h in depth_hits if h.chunk_id in refs]
    return select_cards(breadth=breadth, depth=depth)


@with_phase("compose", ctx_getter=lambda: _ctx())
async def _node_compose(state: ResearchState) -> dict[str, Any]:
    with start_span(
        "research.node.compose",
        **{"aleph.node": "compose", "aleph.project_id": str(state["project_id"])},
    ) as span:
        ctx = _ctx()
        ingested = state.get("ingested") or []
        if not ingested:
            msg = "research ingested no sources; nothing to compose"
            raise RuntimeError(msg)

        cards = await _gather_evidence(state, ingested)
        if not cards:
            # Loud, not degraded. Composing from titles is precisely the defect
            # this node was rebuilt to remove, so "no evidence" fails the run
            # with a diagnosis instead of producing a confident, sourceless
            # report that looks exactly like a good one.
            msg = (
                f"research retrieved no evidence: {len(ingested)} source(s) ingested, "
                "0 retrievable chunks. The sources may still be normalizing or "
                "chunking (see research.evidence.sources_not_indexed), or the "
                "index may be empty."
            )
            raise RuntimeError(msg)
        pack = render_pack(cards)
        span.set_attribute("aleph.evidence.cards", len(cards))
        span.set_attribute("aleph.evidence.chars", len(pack))
        span.set_attribute("aleph.evidence.budget_chars", EVIDENCE_CHAR_BUDGET)

        content = await _chat(
            state,
            capability=Capability.SYNTHESIS,
            purpose="research.compose",
            system=_COMPOSE_SYS,
            user=(
                f"Topic: {state['topic']}\n\n"
                f"Evidence cards (cite inline as [cN], then quote each one you "
                f"cite):\n\n{pack}"
            ),
            max_tokens=4000,
            temperature=0.3,
        )

        body_md, quotes = split_evidence_block(content)
        body_md = sanitize_markers(body_md, len(cards))
        anchored = anchor_body(body_md=body_md, cards=cards, quotes=quotes)
        if not anchored.refs_by_marker:
            msg = (
                f"research composed a report anchored to nothing: {len(cards)} evidence "
                f"card(s) offered, {len(quotes)} quote(s) returned, 0 grounded. An "
                "unanchored research report is the defect, not the fallback."
            )
            raise RuntimeError(msg)
        if anchored.unquoted_markers:
            _log.warning(
                "research.compose.unquoted_markers",
                project_id=str(state["project_id"]),
                markers=anchored.unquoted_markers,
            )

        # Renumber BEFORE style_pass, which renumbers `[cN]` to first-appearance
        # order itself and knows nothing about the marker→chunk map. The old
        # code built that map by list position and then let style_pass shuffle
        # the body underneath it.
        body_md, refs_by_marker = renumber_markers(anchored.body_md, anchored.refs_by_marker)
        body_md = ctx.scholar.style_pass(body_md)
        # style_pass is now a no-op for numbering — the markers are already in
        # first-appearance order — but assert it rather than assume it: a
        # renumbering here would silently re-point every citation at the wrong
        # chunk, which is the exact class of defect being removed.
        stray = set(_CITE_MARKER_RE.findall(body_md))
        known = {m[1:] for m in refs_by_marker}
        if not stray <= known:
            msg = (
                "style_pass renumbered citation markers out from under the "
                f"marker→chunk map (unmapped: {sorted(stray - known)})"
            )
            raise RuntimeError(msg)
        # And keep the map to exactly what the final body cites, so
        # `report.sources` is the sources actually cited rather than the ones
        # cited by a draft that no longer exists.
        present = {f"c{d}" for d in stray}
        refs_by_marker = {m: r for m, r in refs_by_marker.items() if m in present}
        if not refs_by_marker:
            msg = "the style pass removed every citation marker; nothing is anchored"
            raise RuntimeError(msg)

        # Claims come from the FINAL body — after renumbering and after
        # style_pass — so a claim's markers are the markers the commit will
        # see. `build_report` used to pass an empty claim list here, which is why
        # the research path has written zero claims and zero citations to date.
        claims = extract_claims(body_md, known_markers=set(refs_by_marker))
        summary = summarize_body(body_md, fallback=state["topic"])
        span.set_attribute("aleph.evidence.anchored_citations", len(refs_by_marker))
        span.set_attribute("aleph.evidence.claims", len(claims))
        report = build_report(
            topic=state["topic"],
            body_md=body_md,
            summary=summary,
            refs_by_marker=refs_by_marker,
            claims=claims,
        )
        return {"report": report}


@with_phase("synthesize", ctx_getter=lambda: _ctx())
async def _node_synthesize(state: ResearchState) -> dict[str, Any]:
    """Hand the report to the unchanged SynthesisWorkflow; curate committed pages."""
    with start_span(
        "research.node.synthesize",
        **{"aleph.node": "synthesize", "aleph.project_id": str(state["project_id"])},
    ):
        ctx = _ctx()
        workflow = SynthesisWorkflow(
            session_maker=ctx.session_maker,
            litellm=ctx.litellm,
            principal=ctx.principal,
        )
        initial: SynthesisState = {
            "project_id": state["project_id"],
            "topic": state["topic"],
            "report": state["report"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
        }
        run_id = state["agent_run_id"]
        if run_id is not None:
            initial["agent_run_id"] = run_id
        out = await workflow.run(initial)
        committed_page_ids = out.get("committed_page_ids") or []
        for page_id in committed_page_ids:
            # Best-effort knit — never fails the research run.
            try:
                await ctx.enqueue("curate_page_job", str(state["project_id"]), str(page_id))
            except Exception as exc:
                _log.warning(
                    "research.curate_enqueue_failed",
                    page_id=str(page_id),
                    error=str(exc)[:200],
                )
        return {
            "committed_revision_ids": out.get("committed_revision_ids") or [],
            "committed_page_ids": committed_page_ids,
            "proposal_ids": out.get("proposal_ids") or [],
        }


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


class ResearchWorkflow:
    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        litellm: LiteLLMClient,
        principal: Principal,
        scholar: ScholarService,
        asset_store: AssetStore,
        tools: Sequence[BoundTool],
        profile_bindings: dict[str, Any],
        limits: ResearchLimits,
        agent_token_secret: str,
        enqueue: Callable[..., Awaitable[object]],
    ) -> None:
        self._ctx = _Ctx(
            session_maker=session_maker,
            litellm=litellm,
            principal=principal,
            scholar=scholar,
            asset_store=asset_store,
            tools_by_kind={t.kind: t for t in tools},
            profile_bindings=profile_bindings,
            limits=limits,
            agent_token_secret=agent_token_secret,
            enqueue=enqueue,
        )
        # langgraph's StateGraph generics are partially unknown under strict
        # pyright — the same visible boundary the SynthesisWorkflow carries.
        graph: StateGraph = StateGraph(ResearchState)  # pyright: ignore[reportMissingTypeArgument]
        graph.add_node("plan", _node_plan)  # pyright: ignore[reportUnknownMemberType]
        graph.add_node("search", _node_search)  # pyright: ignore[reportUnknownMemberType]
        graph.add_node("ingest", _node_ingest)  # pyright: ignore[reportUnknownMemberType]
        graph.add_node("reflect", _node_reflect)  # pyright: ignore[reportUnknownMemberType]
        graph.add_node("compose", _node_compose)  # pyright: ignore[reportUnknownMemberType]
        graph.add_node("synthesize", _node_synthesize)  # pyright: ignore[reportUnknownMemberType]
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "search")
        graph.add_edge("search", "ingest")
        graph.add_edge("ingest", "reflect")
        graph.add_conditional_edges(
            "reflect", route_after_reflect, {"search": "search", "compose": "compose"}
        )
        graph.add_edge("compose", "synthesize")
        graph.add_edge("synthesize", END)
        self._compiled = graph.compile()  # pyright: ignore[reportUnknownMemberType]

    async def run(self, initial: ResearchState) -> ResearchState:
        token = _active_ctx_var.set(self._ctx)
        try:
            out = await self._compiled.ainvoke(initial)  # pyright: ignore[reportUnknownMemberType]
            return out  # type: ignore[return-value]
        finally:
            _active_ctx_var.reset(token)
