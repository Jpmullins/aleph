"""Wiki-first retrieval router.

Step 1: FTS over WikiIndex + LLM page-selector → top-K selected pages.
Step 2: 1-hop wikilink expansion (deterministic).
Step 3: Composer call (with selected + expanded page bodies).
Step 4: If composer flagged descent → intra-source search, recompose.
Step 5: If composer flagged synthesis → leave it as a flag for Inc 3.

Embeddings appear only in step 4 (intra-source descent). The primary
path (1→3) is wiki-page-selection + wikilink-graph traversal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage
from aleph_observability.tracing import start_span
from aleph_rks.models import Source
from aleph_rks.retrieval import descend_into_source, search_corpus
from aleph_wiki.index_service import IndexService, PageSelectionResult
from aleph_wiki.models import WikiPage, WikiRevision

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_assistant.models import AssistantMessage
    from aleph_db.models.model_profile import ModelProfile
    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


_log = structlog.get_logger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompts"

# An honest miss names its likely cause. Candidate generation is lexical and
# conjunctive — Postgres FTS over each page's title, summary and aliases, with
# `plainto_tsquery`, which requires EVERY term to be present — so "no candidates"
# far more often means the question is phrased in different words than that the
# project lacks the knowledge. Saying "I found nothing" without saying why sends
# the reader off to re-ingest material that is already there.
_MISS_REASON = (
    "no candidate pages: retrieval matches a question's terms against page "
    "titles, summaries and aliases only — never page bodies — and requires every "
    "term to be present. A wording mismatch is a more likely explanation than "
    "missing coverage."
)
_MISS_BODY = (
    "I could not find any wiki page matching that question.\n\n"
    "This is a lexical match against page titles and summaries, so it misses "
    "when a question uses different words than a page's title — including when "
    "the answer is written in the page body. Rephrasing with terms you would "
    "expect in a page title may find it.\n\n"
    "If the project genuinely has no coverage here, `/synthesize` will research "
    "the topic and propose new pages."
)


def _with_degradation(body: str, degraded: Degradation | None) -> str:
    """Append the named degradation to a composed reply.

    In the body, not only in a span attribute: the person reading the answer is
    the one who needs to know it was produced with half the search working.
    """
    if degraded is None:
        return body
    note = _DEGRADED_NOTES.get(degraded)
    return f"{body}\n\n{note}" if note else body


def _prompt(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


RelevanceLabel = Literal["primary", "supporting", "peripheral"]


@dataclass(frozen=True)
class SelectedPage:
    page_id: UUID
    title: str
    slug: str
    summary: str
    body_md: str
    relevance_label: RelevanceLabel
    score: float


@dataclass(frozen=True)
class ExpandedPage:
    page_id: UUID
    title: str
    slug: str
    body_md: str
    expanded_from: UUID  # source page id


@dataclass(frozen=True)
class DescentRequest:
    source_short_id: str
    query_within_source: str


@dataclass(frozen=True)
class DescentChunk:
    chunk_id: UUID
    source_short_id: str
    section_path: str | None
    text: str
    score: float


@dataclass(frozen=True)
class SynthesisRequest:
    concept: str
    missing: str


#: Named degradations. `embedder_unavailable` means the dense half of hybrid
#: search could not run: the answer is lexical-only, which is a real answer and
#: a worse one. It used to be reported as an empty list, which is
#: indistinguishable from "the project knows nothing about this" — the single
#: most misleading thing a retrieval system can say.
Degradation = Literal["embedder_unavailable"]

_DEGRADED_NOTES: dict[str, str] = {
    "embedder_unavailable": (
        "Note: meaning-based (vector) search is unavailable right now — the "
        "embedding model did not answer — so this reply was found by keyword "
        "match alone and may miss passages that say the same thing in different "
        "words."
    ),
}


@dataclass
class RetrievalResult:
    selected_pages: list[SelectedPage]
    expanded_pages: list[ExpandedPage]
    descent_chunks: list[DescentChunk]
    descent_requests: list[DescentRequest]
    synthesis_requests: list[SynthesisRequest]
    coverage_judgment: Literal["ok", "descent_needed", "synthesis_needed", "descent_used"]
    composed_body_md: str
    page_selection_reason: str
    truncated_pages: list[str] = field(default_factory=list)
    #: None when retrieval ran as designed. See :data:`Degradation`.
    degraded: Degradation | None = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class WikiFirstRetrievalRouter:
    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        litellm: LiteLLMClient,
    ) -> None:
        self._maker = session_maker
        self._litellm = litellm
        self._degraded: Degradation | None = None

    async def retrieve(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        thread_id: UUID,
        query: str,
        prior_messages: list[AssistantMessage],
        profile: ModelProfile,
        agent_run_id: UUID | None,
        top_k_pages: int = 8,
        descent_budget_chunks: int = 12,
    ) -> RetrievalResult:
        # Per-call, not per-router: a degradation belongs to the turn that hit it.
        self._degraded: Degradation | None = None
        with start_span(
            "assistant.retrieve",
            **{
                "aleph.project_id": str(project_id),
                "aleph.thread_id": str(thread_id),
            },
        ) as span:
            # Step 1 — candidate generation, over both surfaces in parallel.
            #
            # Pages are the compiled view; chunks are the sources themselves.
            # Searching only pages meant a fact present verbatim in an ingested
            # document was unreachable unless a compile step had lifted it into
            # a page — so the answer depended on what the wiki agent had
            # happened to summarise, not on what the project actually knows.
            async with self._maker() as session:
                index = IndexService(session)
                candidates = await index.select_pages(
                    project_id=project_id,
                    query=query,
                    top_k=top_k_pages * 3,
                )

            corpus_chunks = await self._search_corpus(
                principal=principal,
                project_id=project_id,
                agent_run_id=agent_run_id,
                profile_bindings=profile.bindings_jsonb,
                query=query,
                budget=descent_budget_chunks,
            )
            span.set_attribute("aleph.corpus_chunks", len(corpus_chunks))

            # Nothing on EITHER surface means no coverage, and saying so is the
            # correct answer. This used to fall back to `list_pages` — the
            # project's most-recently-indexed pages, which are not topical
            # matches — and hand them to the page-selector and composer as if
            # they were. That is what kept the retrieval audit check green: the
            # generic probe query missed FTS, the fallback supplied unrelated
            # pages, and the composer wrote plausible prose over them.
            if not candidates and not corpus_chunks:
                span.set_attribute("aleph.coverage_judgment", "synthesis_needed")
                return RetrievalResult(
                    selected_pages=[],
                    expanded_pages=[],
                    descent_chunks=[],
                    descent_requests=[],
                    synthesis_requests=[SynthesisRequest(concept=query, missing=_MISS_REASON)],
                    coverage_judgment="synthesis_needed",
                    composed_body_md=_with_degradation(_MISS_BODY, self._degraded),
                    page_selection_reason=_MISS_REASON,
                    degraded=self._degraded,
                )

            # Step 1b — LLM page-selector picks from the candidates. Skipped
            # entirely when FTS found no pages: there is nothing to select from,
            # and the corpus hits alone are a legitimate answer.
            selected = (
                _SelectionOutcome(pages=[], reason="no page candidates; answering from sources")
                if not candidates
                else await self._select_pages_llm(
                    principal=principal,
                    project_id=project_id,
                    agent_run_id=agent_run_id,
                    profile_bindings=profile.bindings_jsonb,
                    query=query,
                    prior_messages=prior_messages,
                    candidates=candidates,
                    top_k=top_k_pages,
                )
            )

            # Hydrate body_md from the current revision for each selected page.
            page_id_set = {p.page_id for p in selected.pages}
            async with self._maker() as session:
                hydrated_pages = await _hydrate_bodies(session, page_id_set)
            selected_full = [
                SelectedPage(
                    page_id=p.page_id,
                    title=p.title,
                    slug=p.slug,
                    summary=p.summary,
                    body_md=hydrated_pages.get(p.page_id, ""),
                    relevance_label=p.relevance_label,
                    score=p.score,
                )
                for p in selected.pages
            ]

            # Step 2 — 1-hop expansion.
            primary_ids = [p.page_id for p in selected_full if p.relevance_label == "primary"]
            expanded = await self._expand(
                project_id=project_id,
                primary_ids=primary_ids,
                already_selected=page_id_set,
                limit=top_k_pages,
            )

            # Step 3 — composer.
            composer_out = await self._compose(
                principal=principal,
                project_id=project_id,
                agent_run_id=agent_run_id,
                profile_bindings=profile.bindings_jsonb,
                query=query,
                prior_messages=prior_messages,
                selected=selected_full,
                expanded=expanded,
                descent_chunks=corpus_chunks,
            )

            # Step 4 — descent if requested.
            descent_chunks: list[DescentChunk] = []
            coverage: Literal["ok", "descent_needed", "synthesis_needed", "descent_used"]
            if composer_out["descent_requests"]:
                descent_chunks = await self._do_descent(
                    principal=principal,
                    project_id=project_id,
                    agent_run_id=agent_run_id,
                    profile_bindings=profile.bindings_jsonb,
                    requests=composer_out["descent_requests"],
                    budget=descent_budget_chunks,
                )
                if descent_chunks:
                    # Re-compose with the extra passages folded in alongside
                    # what corpus search already found.
                    descent_chunks = _dedupe_chunks([*corpus_chunks, *descent_chunks])
                    composer_out = await self._compose(
                        principal=principal,
                        project_id=project_id,
                        agent_run_id=agent_run_id,
                        profile_bindings=profile.bindings_jsonb,
                        query=query,
                        prior_messages=prior_messages,
                        selected=selected_full,
                        expanded=expanded,
                        descent_chunks=descent_chunks,
                    )
                    coverage = "descent_used"
                else:
                    coverage = "descent_needed"
            elif composer_out["synthesis_requests"]:
                coverage = "synthesis_needed"
            else:
                coverage = "ok"

            span.set_attribute("aleph.coverage_judgment", coverage)
            span.set_attribute(
                "aleph.selected_pages", json.dumps([str(p.page_id) for p in selected_full])
            )
            return RetrievalResult(
                selected_pages=selected_full,
                expanded_pages=expanded,
                descent_chunks=descent_chunks or corpus_chunks,
                descent_requests=composer_out["descent_requests"],
                synthesis_requests=composer_out["synthesis_requests"],
                coverage_judgment=coverage,
                composed_body_md=_with_degradation(composer_out["body_md"], self._degraded),
                page_selection_reason=selected.reason,
                degraded=self._degraded,
            )

    # ---- step helpers ------------------------------------------------------

    async def _search_corpus(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        profile_bindings: dict,
        query: str,
        budget: int,
    ) -> list[DescentChunk]:
        """Hybrid search over the project's source chunks.

        Runs up front rather than only when the composer asks for it. Descent
        was reactive — the composer had to already have a page citing a source
        before it could request more of that source — so a fact present verbatim
        in an ingested document was unreachable until some compile step had
        lifted it into a page.

        An embedding failure degrades rather than failing the whole turn — but it
        degrades *out loud*. Returning `[]` on a dead embedder was the same
        answer as "this project has nothing on that", and the two are not the
        same at all: the first is an outage, the second is a fact. The caller
        gets a named degradation so the composed reply can say which one it is.

        Degraded retrieval is still real retrieval: the lexical leg needs no
        model, so a zero vector plus the query text still returns keyword hits.
        """
        query_vector: list[float] | None = None
        try:
            embedded = await self._litellm.embed(
                principal=principal,
                project_id=project_id,
                agent_run_id=agent_run_id,
                profile_bindings=profile_bindings,
                input=[query],
                purpose="assistant.corpus_search.query_embed",
            )
            if embedded.embeddings:
                query_vector = embedded.embeddings[0]
        except Exception as exc:
            _log.warning(
                "assistant.corpus_search.embed_failed",
                project_id=str(project_id),
                error=f"{type(exc).__name__}: {exc}",
            )

        if query_vector is None:
            # `search_corpus` reads None as "lexical leg only". Handing it a
            # zero vector instead would look equivalent and quietly fuse an
            # arbitrary dense ranking into the result.
            self._degraded = "embedder_unavailable"

        async with self._maker() as session:
            hits = await search_corpus(
                session,
                project_id=project_id,
                query_text=query,
                query_embedding=query_vector,
                top_k=budget,
            )
            short_ids = await _source_short_ids(session, {h.source_id for h in hits if h.source_id})

        return [
            DescentChunk(
                chunk_id=h.chunk_id,
                source_short_id=short_ids.get(h.source_id, "") if h.source_id else "",
                section_path=h.section_path,
                text=h.text,
                score=h.score,
            )
            for h in hits
        ]

    async def _select_pages_llm(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        profile_bindings: dict,
        query: str,
        prior_messages: list[AssistantMessage],
        candidates: list[PageSelectionResult],
        top_k: int,
    ) -> _SelectionOutcome:
        if not candidates:
            return _SelectionOutcome(pages=[], reason="no candidates")

        cand_payload = [
            {
                "page_id": str(c.page_id),
                "title": c.title,
                "slug": c.slug,
                "summary": c.summary,
                "wikilinks_out": [w.get("dst_title") for w in (c.wikilinks_out or [])[:5]],
                "score": c.score,
                "page_kind": c.page_kind,
                "is_stub": c.is_stub,
            }
            for c in candidates
        ]

        recent = []
        for m in prior_messages[-6:]:
            recent.append({"role": m.role, "body": m.body_md[:1000]})

        user_payload = json.dumps(
            {
                "query": query,
                "top_k": top_k,
                "recent_turns": recent,
                "candidates": cand_payload,
            },
            indent=2,
        )

        resp = await self._litellm.chat(
            principal=principal,
            project_id=project_id,
            agent_run_id=agent_run_id,
            capability=Capability.PAGE_SELECTION,
            profile_bindings=profile_bindings,
            messages=[
                ChatMessage(role="system", content=_prompt("page_selector")),
                ChatMessage(role="user", content=user_payload),
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1024,
            purpose="assistant.page_selection",
        )
        content = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        parsed = _safe_json(content)
        selected_raw = parsed.get("selected", []) if isinstance(parsed, dict) else []
        reason = parsed.get("reason", "") if isinstance(parsed, dict) else ""

        cand_by_id = {str(c.page_id): c for c in candidates}
        out: list[SelectedPage] = []
        for item in selected_raw[:top_k]:
            pid = item.get("page_id") if isinstance(item, dict) else None
            label = item.get("relevance_label") if isinstance(item, dict) else None
            cand = cand_by_id.get(pid) if isinstance(pid, str) else None
            if cand is None or label not in ("primary", "supporting", "peripheral"):
                continue
            out.append(
                SelectedPage(
                    page_id=cand.page_id,
                    title=cand.title,
                    slug=cand.slug,
                    summary=cand.summary,
                    body_md="",  # hydrated by caller
                    relevance_label=label,
                    score=cand.score,
                )
            )
        # The LLM returned nothing usable, but every candidate here is a real
        # FTS hit — the caller short-circuits when FTS is empty. Surface the
        # top few as "supporting" (never "primary", so they are not 1-hop
        # expanded or treated as authoritative grounding).
        if not out:
            for c in candidates[: min(top_k, 3)]:
                out.append(
                    SelectedPage(
                        page_id=c.page_id,
                        title=c.title,
                        slug=c.slug,
                        summary=c.summary,
                        body_md="",
                        relevance_label="supporting",
                        score=c.score,
                    )
                )
        return _SelectionOutcome(pages=out, reason=str(reason))

    async def _expand(
        self,
        *,
        project_id: UUID,
        primary_ids: list[UUID],
        already_selected: set[UUID],
        limit: int,
    ) -> list[ExpandedPage]:
        if not primary_ids:
            return []
        async with self._maker() as session:
            from aleph_wiki.models import WikiLink

            # WikiLink rows are per-revision and never deleted, so a page that
            # has been rewritten N times has N sets of outgoing links in the
            # table. Without the src_revision_id join this walked the union of
            # every revision a page ever had, so a link removed three revisions
            # ago kept pulling its target into the answer context forever.
            # index_service, routes/wiki and the mechanical reviewer all filter
            # correctly; this — the one path that feeds answers — did not.
            stmt = (
                select(WikiLink)
                .join(WikiPage, WikiPage.id == WikiLink.src_page_id)
                .where(
                    WikiLink.project_id == project_id,
                    WikiLink.src_page_id.in_(primary_ids),
                    WikiLink.src_revision_id == WikiPage.current_revision_id,
                    WikiLink.dst_page_id.is_not(None),
                )
                .order_by(WikiLink.occurrences.desc())
            )
            link_rows = list((await session.execute(stmt)).scalars().all())

            dst_ids: list[UUID] = []
            seen: set[UUID] = set(already_selected)
            for link in link_rows:
                if link.dst_page_id is None or link.dst_page_id in seen:
                    continue
                dst_ids.append(link.dst_page_id)
                seen.add(link.dst_page_id)
                if len(dst_ids) >= limit:
                    break

            if not dst_ids:
                return []

            hydrated = await _hydrate_bodies(session, set(dst_ids))
            pages_stmt = select(WikiPage).where(WikiPage.id.in_(dst_ids))
            pages = {p.id: p for p in (await session.execute(pages_stmt)).scalars().all()}

        link_by_dst: dict[UUID, UUID] = {}
        for link in link_rows:
            if link.dst_page_id in dst_ids and link.dst_page_id not in link_by_dst:
                link_by_dst[link.dst_page_id] = link.src_page_id
        out: list[ExpandedPage] = []
        for dst_id in dst_ids:
            page = pages.get(dst_id)
            if page is None:
                continue
            out.append(
                ExpandedPage(
                    page_id=dst_id,
                    title=page.title,
                    slug=page.slug,
                    body_md=hydrated.get(dst_id, ""),
                    expanded_from=link_by_dst.get(dst_id, dst_id),
                )
            )
        return out

    async def _compose(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        profile_bindings: dict,
        query: str,
        prior_messages: list[AssistantMessage],
        selected: list[SelectedPage],
        expanded: list[ExpandedPage],
        descent_chunks: list[DescentChunk],
    ) -> dict[str, Any]:
        ctx_pages = []
        for p in selected:
            ctx_pages.append(
                {
                    "page_id": str(p.page_id),
                    "title": p.title,
                    "slug": p.slug,
                    "relevance": p.relevance_label,
                    "body_md": p.body_md[:16_000],
                }
            )
        ctx_expanded = []
        for p in expanded:
            ctx_expanded.append(
                {
                    "page_id": str(p.page_id),
                    "title": p.title,
                    "slug": p.slug,
                    "body_md": p.body_md[:6_000],
                }
            )
        ctx_descent = [
            {
                "source_short_id": d.source_short_id,
                "section_path": d.section_path,
                "text": d.text[:4_000],
                "chunk_id": str(d.chunk_id),
            }
            for d in descent_chunks
        ]
        recent = [{"role": m.role, "body": m.body_md[:1500]} for m in prior_messages[-8:]]

        payload = json.dumps(
            {
                "query": query,
                "recent_turns": recent,
                "selected_pages": ctx_pages,
                "expanded_pages": ctx_expanded,
                "descent_chunks": ctx_descent,
            },
            indent=2,
        )

        resp = await self._litellm.chat(
            principal=principal,
            project_id=project_id,
            agent_run_id=agent_run_id,
            capability=Capability.SYNTHESIS,
            profile_bindings=profile_bindings,
            messages=[
                ChatMessage(role="system", content=_prompt("composer")),
                ChatMessage(role="user", content=payload),
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=2048,
            purpose="assistant.compose",
        )
        content = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        parsed = _safe_json(content)
        body_md = parsed.get("body_md", "") if isinstance(parsed, dict) else ""
        if not body_md.strip():
            body_md = (
                "I couldn't compose an answer from the current wiki coverage. "
                "If you ran `/synthesize`, that would research and add new wiki pages."
            )
        raw_d = parsed.get("descent_requests", []) if isinstance(parsed, dict) else []
        raw_s = parsed.get("synthesis_requests", []) if isinstance(parsed, dict) else []
        descent: list[DescentRequest] = []
        for r in raw_d:
            if not isinstance(r, dict):
                continue
            sid = r.get("source_short_id")
            q = r.get("query_within_source")
            if isinstance(sid, str) and isinstance(q, str) and sid.strip() and q.strip():
                descent.append(DescentRequest(source_short_id=sid.strip(), query_within_source=q))
        synth: list[SynthesisRequest] = []
        for r in raw_s:
            if not isinstance(r, dict):
                continue
            concept = r.get("concept")
            missing = r.get("missing")
            if isinstance(concept, str) and isinstance(missing, str):
                synth.append(SynthesisRequest(concept=concept, missing=missing))
        return {
            "body_md": body_md,
            "descent_requests": descent,
            "synthesis_requests": synth,
        }

    async def _do_descent(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        profile_bindings: dict,
        requests: list[DescentRequest],
        budget: int,
    ) -> list[DescentChunk]:
        if not requests:
            return []
        remaining = budget
        out: list[DescentChunk] = []
        # Resolve short_id → source_id once.
        async with self._maker() as session:
            short_to_id = {}
            stmt = select(Source).where(
                Source.project_id == project_id,
                Source.short_id.in_([r.source_short_id for r in requests]),
            )
            for s in (await session.execute(stmt)).scalars().all():
                short_to_id[s.short_id] = s.id

        for req in requests:
            if remaining <= 0:
                break
            source_id = short_to_id.get(req.source_short_id)
            if source_id is None:
                continue
            # Embed the question.
            embed_resp = await self._litellm.embed(
                principal=principal,
                project_id=project_id,
                agent_run_id=agent_run_id,
                profile_bindings=profile_bindings,
                input=[req.query_within_source],
                purpose="assistant.descent.query_embed",
            )
            if not embed_resp.embeddings:
                continue
            async with self._maker() as session:
                hits = await descend_into_source(
                    session,
                    project_id=project_id,
                    source_id=source_id,
                    query_text=req.query_within_source,
                    query_embedding=embed_resp.embeddings[0],
                    top_k=min(4, remaining),
                )
            for h in hits:
                out.append(
                    DescentChunk(
                        chunk_id=h.chunk_id,
                        source_short_id=req.source_short_id,
                        section_path=h.section_path,
                        text=h.text,
                        score=h.score,
                    )
                )
                remaining -= 1
                if remaining <= 0:
                    break
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass
class _SelectionOutcome:
    pages: list[SelectedPage]
    reason: str


def _safe_json(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


async def _source_short_ids(session: AsyncSession, source_ids: set[UUID]) -> dict[UUID, str]:
    """Map source ids to the short ids the composer cites."""
    if not source_ids:
        return {}
    rows = (
        await session.execute(select(Source.id, Source.short_id).where(Source.id.in_(source_ids)))
    ).all()
    return {row.id: row.short_id for row in rows}


def _dedupe_chunks(chunks: list[DescentChunk]) -> list[DescentChunk]:
    """First occurrence wins, so corpus hits keep their (higher) fused rank."""
    seen: set[UUID] = set()
    out: list[DescentChunk] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        out.append(chunk)
    return out


async def _hydrate_bodies(session: AsyncSession, page_ids: set[UUID]) -> dict[UUID, str]:
    if not page_ids:
        return {}
    page_stmt = select(WikiPage).where(WikiPage.id.in_(page_ids))
    pages = list((await session.execute(page_stmt)).scalars().all())
    rev_ids = [p.current_revision_id for p in pages if p.current_revision_id is not None]
    if not rev_ids:
        return {p.id: "" for p in pages}
    rev_stmt = select(WikiRevision).where(WikiRevision.id.in_(rev_ids))
    revs = {r.id: r for r in (await session.execute(rev_stmt)).scalars().all()}
    out: dict[UUID, str] = {}
    for p in pages:
        rev = revs.get(p.current_revision_id) if p.current_revision_id else None
        out[p.id] = rev.body_md if rev else ""
    return out
