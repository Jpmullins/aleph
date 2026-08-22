"""Chunk retrieval — pgvector cosine + Postgres FTS, fused by reciprocal rank.

Two entry points over the same machinery:

* :func:`search_corpus` — across every source in a project.
* :func:`descend_into_source` — the same search, pinned to one source, for when
  an answer cites a specific document and needs more of it.

`descend_into_source` used to be the only one. The corpus-wide search was
withheld on the rule that embeddings must never be first-line retrieval, and the
wiki index that replaced it covered page titles, summaries and aliases but never
page bodies — so the system had no way to find text by the words it was written
in. The restriction was the whole defect; the retriever underneath it was fine.

Two changes beyond removing the predicate:

* An OR'd tsquery instead of ``plainto_tsquery``. Every Postgres parser —
  ``plainto_``, ``websearch_to_`` — conjoins unquoted terms, so a
  natural-language question had to contain every content word to match anything
  at all. See :func:`aleph_core.tsquery.or_tsquery`.
* Reciprocal rank fusion instead of a weighted score blend. Cosine similarity
  and ``ts_rank`` have no shared scale, and the old normalisation (divide by the
  batch maximum) made a chunk's score depend on which other chunks came back.
  See :mod:`aleph_core.rrf`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select, text

from aleph_core.rrf import fuse
from aleph_core.tsquery import or_tsquery
from aleph_observability.tracing import start_span
from aleph_rks.models import EMBEDDING_DIM, DocumentChunk, RetrievalIndexRecord, Source

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_rks.rerank import Reranker


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: UUID
    ordinal: int
    text: str
    section_path: str | None
    #: The FUSED rank score. Comparable within one result list and meaningless
    #: across two: RRF is a function of rank, so the top hit scores the same
    #: whether it is a perfect match or the least-bad of a bad set.
    score: float
    source_id: UUID | None = None
    #: Absolute relevance, from the legs that produce it. `None` when that leg
    #: did not return this chunk.
    #:
    #: These exist because RRF cannot answer "is anything here actually
    #: relevant". Both legs computed them and both threw them away — the dense
    #: leg ordered by `cosine_distance` and selected five columns not including
    #: it, and the lexical leg ordered by `ts_rank` and did the same. So the one
    #: signal that could tell a real match from the nearest irrelevant passage
    #: was discarded at the point it was calculated.
    cosine_distance: float | None = None
    lexical_rank: float | None = None
    #: What the reranker thought of this chunk, on the reranker's own scale.
    #: `None` when no reranker ran, or when one ran and did not judge this
    #: chunk (see `aleph_rks.rerank.apply_ranking`, which keeps unjudged
    #: candidates behind the judged ones rather than deleting them).
    rerank_score: float | None = None
    #: Position after reranking, 0-based. `None` when no reranker ran. Kept
    #: alongside the fused `score` rather than replacing it: the two are
    #: different scales, and overwriting one with the other would silently
    #: change what every existing reader of `.score` is looking at.
    rerank_position: int | None = None


#: How many chunks any one source may contribute to a corpus-wide result.
#: Without a cap, a single long document with many near-identical chunks fills
#: the whole window and the answer sees one source's view of the question.
DEFAULT_PER_SOURCE_CAP = 3

#: How many fused candidates a reranker is shown. Defined here rather than
#: imported from `aleph_rks.rerank` because that module imports this one for
#: `ChunkHit`; the value is restated in the reranker's own docstring with the
#: cost reasoning behind it.
DEFAULT_RERANK_WINDOW = 40


# ---------------------------------------------------------------------------
# Vector scan configuration
# ---------------------------------------------------------------------------
#
# pgvector's HNSW scan is bounded by `hnsw.ef_search` (default 40) — the size
# of the candidate list it keeps while descending the graph. Everything below
# exists because that default is a *global* bound applied BEFORE the
# `project_id` filter: the index returns its candidates, Postgres then throws
# away the ones belonging to other projects, and a query that asked for 80 rows
# gets whatever survives. Nothing anywhere reports the shortfall — the dense leg
# simply contributes a shorter ranking to RRF, so fusion silently becomes more
# lexical the more projects share the store.
#
# Repo-wide, `ef_search` and `iterative_scan` appeared exactly zero times before
# WS-RS6. The index was created with `m=16, ef_construction=64` and then queried
# at whatever the server happened to default to.

#: `ef_search` as a multiple of the rows the query asks for.
#:
#: Four, because the filter is what eats the margin: with N projects sharing
#: the table, roughly 1/N of the candidate list survives the `project_id`
#: predicate, and a multiplier of one guarantees a short result whenever more
#: than one project exists. Four is a compromise, not a derivation — measure
#: before raising it, because ef_search is the main cost knob on the dense leg.
EF_SEARCH_MULTIPLIER = 4

#: Never search a narrower candidate list than pgvector's own default.
EF_SEARCH_FLOOR = 40

#: And never a wider one than this. `ef_search` is linear-ish in scan cost, and
#: an unbounded multiple of a caller-supplied `top_k` is a way for one search to
#: cost a minute.
EF_SEARCH_CEILING = 1_000

#: Hard stop for the iterative scan, in tuples. pgvector's own default (20,000)
#: restated here so the bound is visible at the call site rather than being
#: whatever the server was built with: iterative scan means "keep going until
#: enough rows pass the filter", and without a ceiling a filter that matches
#: almost nothing walks the whole index.
MAX_SCAN_TUPLES = 20_000

#: First pgvector release with `hnsw.iterative_scan`.
_ITERATIVE_SCAN_MIN_VERSION = (0, 8)

#: Probed once per process. `hnsw` is a *reserved* GUC prefix once pgvector's
#: library is loaded, so setting `hnsw.iterative_scan` against an older
#: extension does not degrade — it raises, and in Postgres a raise inside a
#: transaction poisons the whole transaction. So the version is read from the
#: catalogue (which needs no library load) before anything is set.
_iterative_scan_supported: bool | None = None


def reset_scan_support_cache() -> None:
    """Forget the pgvector version probe. For tests that swap databases."""
    global _iterative_scan_supported
    _iterative_scan_supported = None


def ef_search_for(fetch: int) -> int:
    """The `hnsw.ef_search` a dense leg asking for `fetch` rows should run at.

    Public and pure so a test can assert the value the server reports equals
    the value production computed, rather than a constant copied into the test
    — which would pass just as happily if production stopped setting it.
    """
    return min(max(fetch * EF_SEARCH_MULTIPLIER, EF_SEARCH_FLOOR), EF_SEARCH_CEILING)


def iterative_scan_supported(extversion: str | None) -> bool:
    """Does this pgvector version have `hnsw.iterative_scan`?

    Split out as a pure function because the interesting cases — no extension
    at all, `0.7.4`, a version string with a suffix — are not worth a database
    to test.
    """
    if not extversion:
        return False
    parts: list[int] = []
    for piece in extversion.split(".")[:2]:
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return False
        parts.append(int(digits))
    return len(parts) == 2 and tuple(parts) >= _ITERATIVE_SCAN_MIN_VERSION


async def _configure_vector_scan(session: AsyncSession, *, fetch: int) -> int:
    """Set the scan GUCs for this transaction. Returns the `ef_search` used.

    `set_config(..., is_local => true)` rather than `SET LOCAL` because the
    latter cannot take a bind parameter, and the alternative — interpolating an
    integer into SQL text — is the shape of a defect even when the integer is
    trustworthy today.
    """
    global _iterative_scan_supported
    ef = ef_search_for(fetch)
    await session.execute(select(func.set_config("hnsw.ef_search", str(ef), True)))
    if _iterative_scan_supported is None:
        row = (
            await session.execute(
                text("select extversion from pg_extension where extname = 'vector'")
            )
        ).scalar_one_or_none()
        _iterative_scan_supported = iterative_scan_supported(str(row) if row is not None else None)
        if not _iterative_scan_supported:
            _log.warning(
                "rks.search.iterative_scan_unavailable",
                pgvector_version=row,
                impact=(
                    "the dense leg may return fewer than the requested rows once a "
                    "project is a small share of document_chunks; upgrade pgvector to "
                    "0.8 or later"
                ),
            )
    if _iterative_scan_supported:
        # `strict_order`, not `relaxed_order`. Relaxed is faster and returns
        # results slightly out of distance order — and the dense leg's output is
        # consumed by RRF, which reads nothing but the ORDER. Out-of-order rows
        # would corrupt the fused ranking in a way no assertion here could see.
        await session.execute(select(func.set_config("hnsw.iterative_scan", "strict_order", True)))
        await session.execute(
            select(func.set_config("hnsw.max_scan_tuples", str(MAX_SCAN_TUPLES), True))
        )
    return ef


def _scope(*, project_id: UUID, source_id: UUID | None) -> list[Any]:
    scope: list[Any] = [DocumentChunk.project_id == project_id]
    if source_id is not None:
        scope.append(DocumentChunk.source_id == source_id)
    return scope


def fetch_for(top_k: int) -> int:
    """How many rows each leg asks for, given the caller's `top_k`.

    Both legs over-fetch so fusion has something to disagree about — a fused
    list built from two 8-item rankings is mostly just the first ranking.
    Named rather than inlined so the scan configuration and the tests that
    check it derive the same number production uses instead of restating it.
    """
    return max(top_k * 4, 40)


async def dense_candidates(
    session: AsyncSession,
    *,
    project_id: UUID,
    source_id: UUID | None = None,
    embedding: list[float],
    top_k: int | None = None,
) -> list[Any]:
    """The dense leg on its own: `fetch_for(top_k)` nearest chunks, in order.

    Extracted from `_hybrid_search` so the row count it returns can be asserted
    directly. Fused output cannot answer "did the dense leg come back short" —
    a short dense ranking merges into a fused list that looks entirely normal,
    which is how a filtered HNSW scan silently turns hybrid search into keyword
    search (see the scan configuration above).
    """
    fetch = fetch_for(top_k if top_k is not None else 8)
    # Same transaction as the query below, which is the whole point: a GUC set
    # on a different connection (or committed away by an intervening commit) is
    # a setting that looks configured and is not.
    await _configure_vector_scan(session, fetch=fetch)
    distance = DocumentChunk.embedding.cosine_distance(embedding)  # type: ignore[attr-defined]
    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.ordinal,
            DocumentChunk.text,
            DocumentChunk.section_path,
            DocumentChunk.source_id,
            # SELECTED, not just ordered by. It was computed and discarded.
            distance.label("distance"),
        )
        # `embedding IS NOT NULL` is load-bearing, not defensive. A chunk is
        # written before it is embedded, so an un-embedded chunk is a normal
        # row — and ordering by `cosine_distance` over a NULL vector is
        # undefined in Postgres, not simply last. Without this predicate a
        # degraded index makes the dense leg return an arbitrary page of rows,
        # which RRF then fuses as though it were a ranking. An empty dense leg
        # is the honest answer; `search_corpus` still answers from the lexical
        # leg, which needs no model.
        .where(
            *_scope(project_id=project_id, source_id=source_id), DocumentChunk.embedding.isnot(None)
        )
        .order_by(distance)
        .limit(fetch)
    )
    return list((await session.execute(stmt)).all())


async def _hybrid_search(
    session: AsyncSession,
    *,
    project_id: UUID,
    query_text: str,
    query_embedding: list[float] | None,
    top_k: int,
    source_id: UUID | None,
    per_source_cap: int | None,
) -> list[ChunkHit]:
    """Dense and lexical rankings over chunks, fused by RRF.

    ``source_id`` pins the search to one document; ``None`` searches the whole
    project. Both legs over-fetch relative to ``top_k`` so fusion has something
    to disagree about — a fused list built from two 8-item rankings is mostly
    just the first ranking.

    ``query_embedding=None`` means **run the lexical leg only**, which is what a
    caller whose embedder is unavailable should ask for. Passing a zero vector
    instead looks equivalent and is not: cosine distance to the zero vector is
    degenerate, so the dense leg returns an arbitrary page of rows and RRF fuses
    that noise as though it were a ranking. An absent leg is honest; a
    meaningless one is worse than none.
    """
    scope = _scope(project_id=project_id, source_id=source_id)
    fetch = fetch_for(top_k)

    dense_rows: list[Any] = []
    if query_embedding is not None:
        dense_rows = await dense_candidates(
            session,
            project_id=project_id,
            source_id=source_id,
            embedding=query_embedding,
            top_k=top_k,
        )

    # OR the terms. Every Postgres parser conjoins unquoted words, so a whole
    # question would otherwise have to match every content word to return
    # anything. `or_tsquery` widens the candidate set; ts_rank still orders a
    # full match above a partial one. See aleph_core.tsquery.
    tsquery = or_tsquery(query_text)
    rank = func.ts_rank(DocumentChunk.text_tsv, tsquery)
    lexical_stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.ordinal,
            DocumentChunk.text,
            DocumentChunk.section_path,
            DocumentChunk.source_id,
            rank.label("rank"),
        )
        .where(*scope, DocumentChunk.text_tsv.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(fetch)
    )
    lexical_rows = list((await session.execute(lexical_stmt)).all())

    by_id = {row.id: row for row in [*dense_rows, *lexical_rows]}
    distance_by_id = {row.id: float(row.distance) for row in dense_rows}
    rank_by_id = {row.id: float(row.rank) for row in lexical_rows}
    fused = fuse([[r.id for r in dense_rows], [r.id for r in lexical_rows]])

    hits: list[ChunkHit] = []
    per_source: dict[UUID | None, int] = {}
    for chunk_id, score in fused:
        row = by_id[chunk_id]
        if per_source_cap is not None:
            used = per_source.get(row.source_id, 0)
            if used >= per_source_cap:
                continue
            per_source[row.source_id] = used + 1
        hits.append(
            ChunkHit(
                chunk_id=chunk_id,
                ordinal=row.ordinal,
                text=row.text,
                section_path=row.section_path,
                score=score,
                source_id=row.source_id,
                cosine_distance=distance_by_id.get(chunk_id),
                lexical_rank=rank_by_id.get(chunk_id),
            )
        )
        if len(hits) >= top_k:
            break
    return hits


async def _fuse_then_rerank(
    session: AsyncSession,
    *,
    project_id: UUID,
    query_text: str,
    query_embedding: list[float] | None,
    top_k: int,
    source_id: UUID | None,
    per_source_cap: int | None,
    reranker: Reranker | None,
    rerank_window: int,
) -> list[ChunkHit]:
    """Fusion, then the second pass — and a span that says which one ran.

    The span is not optional decoration. `Capability.RERANK` shipped as a row in
    the Settings drawer with nothing behind it, and the way that survived is
    that a search with no reranker and a search with one produce
    indistinguishable output. `retrieval.rerank.skipped` carries the REASON, in
    a sentence, so "reranking is off" can never again be something you have to
    infer from result quality.
    """
    with start_span(
        "rks.search",
        **{
            "aleph.project_id": str(project_id),
            "retrieval.scope": "source" if source_id else "corpus",
            "retrieval.top_k": top_k,
            "retrieval.dense": query_embedding is not None,
        },
    ) as span:
        skipped = REASON_RERANK_NOT_REQUESTED if reranker is None else reranker.skipped_reason
        # Candidates, not results. A reranker that only ever sees `top_k` rows
        # can reorder them and nothing more — the hit sitting at rank 25 is the
        # one it exists to promote, so the window is what gets fetched and
        # `top_k` is applied after the judgement.
        window = top_k if skipped is not None else max(rerank_window, top_k)
        fused = await _hybrid_search(
            session,
            project_id=project_id,
            query_text=query_text,
            query_embedding=query_embedding,
            top_k=window,
            source_id=source_id,
            per_source_cap=per_source_cap,
        )
        span.set_attribute("retrieval.fused_hits", len(fused))
        if reranker is None or skipped is not None:
            span.set_attribute("retrieval.rerank.skipped", skipped or REASON_RERANK_NOT_REQUESTED)
            return fused[:top_k]
        span.set_attribute("retrieval.rerank.candidates", len(fused))
        ranked = await reranker.rank(query=query_text, hits=fused, top_k=top_k)
        span.set_attribute("retrieval.rerank.backend", reranker.name)
        span.set_attribute("retrieval.rerank.returned", len(ranked))
        # Zero results after a non-empty candidate list is the reranker saying
        # "none of this answers the question". Recorded as its own attribute
        # because it is the one outcome that looks like a broken retriever and
        # is not — see `aleph_rks.rerank.apply_ranking`.
        span.set_attribute("retrieval.rerank.abstained", bool(fused) and not ranked)
        return ranked


#: Why no second pass ran, when the caller never asked for one. The unbound-
#: capability wording lives in `aleph_rks.rerank`, next to the code that
#: detects it.
REASON_RERANK_NOT_REQUESTED = "the caller passed no reranker to search_corpus"


async def search_corpus(
    session: AsyncSession,
    *,
    project_id: UUID,
    query_text: str,
    query_embedding: list[float] | None,
    top_k: int = 8,
    per_source_cap: int | None = DEFAULT_PER_SOURCE_CAP,
    reranker: Reranker | None = None,
    rerank_window: int = DEFAULT_RERANK_WINDOW,
) -> list[ChunkHit]:
    """Search every source in a project. This is first-line retrieval.

    ``query_embedding=None`` searches lexically only — the honest degraded mode
    when no embedder is available.

    ``reranker`` is a second pass over the fused candidates
    (:mod:`aleph_rks.rerank`). Default ``None`` — reranking costs a model call
    per search, so it is opt-in per call site rather than a default that every
    existing caller silently starts paying for. Whichever way it goes, the
    ``rks.search`` span says which, and why.
    """
    return await _fuse_then_rerank(
        session,
        project_id=project_id,
        query_text=query_text,
        query_embedding=query_embedding,
        top_k=top_k,
        source_id=None,
        per_source_cap=per_source_cap,
        reranker=reranker,
        rerank_window=rerank_window,
    )


async def descend_into_source(
    session: AsyncSession,
    *,
    project_id: UUID,
    source_id: UUID,
    query_text: str,
    query_embedding: list[float] | None,
    top_k: int = 8,
    reranker: Reranker | None = None,
    rerank_window: int = DEFAULT_RERANK_WINDOW,
) -> list[ChunkHit]:
    """The same search, pinned to one source. No per-source cap: there is one."""
    return await _fuse_then_rerank(
        session,
        project_id=project_id,
        query_text=query_text,
        query_embedding=query_embedding,
        top_k=top_k,
        source_id=source_id,
        per_source_cap=None,
        reranker=reranker,
        rerank_window=rerank_window,
    )


async def get_index_record(session: AsyncSession, source_id: UUID) -> RetrievalIndexRecord | None:
    stmt = select(RetrievalIndexRecord).where(RetrievalIndexRecord.source_id == source_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def needs_reembed(
    session: AsyncSession, source_id: UUID, *, current_embedder_model: str
) -> bool:
    """Is this source's index behind the current embedder AND representation?

    `current_embedder_model` is a model name; the comparison is against the
    recorded *signature* (`aleph_rks.indexing.index_signature`), so turning
    contextual embedding on marks every source stale without a schema change.
    """
    from aleph_rks.indexing import index_signature

    rec = await get_index_record(session, source_id)
    if rec is None:
        return True
    return rec.embedder_model != index_signature(current_embedder_model)


async def reembed_for_project(
    session: AsyncSession,
    *,
    project_id: UUID,
    client: Any,
    principal: Any,
    profile_bindings: dict,
    purpose: str = "rks.reembed",
) -> tuple[int, int]:
    """Re-embed chunks of any source whose stored embedder model differs from
    the project's current `embedding` binding (embedder-model drift repair).

    Bounded + idempotent: only sources whose `RetrievalIndexRecord.embedder_model`
    is stale are touched; once re-embedded they match and are skipped on re-run.
    Re-embedding goes through `embed_texts` → `LiteLLMClient.embed`, so it writes
    `ModelCall` + `CostLedgerEvent`. Returns (sources_reembedded, chunks_reembedded).
    """
    from aleph_core.errors import ValidationFailed
    from aleph_core.time import utcnow
    from aleph_models.profile import resolve_binding
    from aleph_rks.embedding import embed_texts, embedding_dim_mismatch
    from aleph_rks.indexing import embedding_text, index_signature

    # A project with no `embedding` binding is the normal state between project
    # creation and autoconfigure — templates ship no model name. Nothing to
    # re-embed to, so this is a no-op rather than an exception on a background
    # job nobody is watching.
    try:
        # The signature, so a representation change (contextual embedding on or
        # off) is as stale as a model change. Without it, flipping the
        # representation leaves every existing source looking current while its
        # vectors were built from a different string.
        current_model = index_signature(resolve_binding(profile_bindings, "embedding").model)
    except ValidationFailed:
        _log.warning("rks.reembed.no_embedding_binding", project_id=str(project_id))
        return 0, 0
    # `embedder_model IS NULL` is the degraded case, and it has to be named
    # explicitly: in SQL `NULL != 'x'` is NULL, not true, so a `lexical_only`
    # index — the state a source lands in when the embedder was unreachable —
    # would never be selected for repair by an inequality alone. That would
    # make the repair path unable to repair the only failure it exists for.
    stale = list(
        (
            await session.execute(
                select(RetrievalIndexRecord).where(
                    RetrievalIndexRecord.project_id == project_id,
                    or_(
                        RetrievalIndexRecord.embedder_model.is_(None),
                        RetrievalIndexRecord.embedder_model != current_model,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    sources_done = 0
    chunks_done = 0
    dim_blocked = 0
    for rec in stale:
        chunks = list(
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.source_id == rec.source_id)
                    .order_by(DocumentChunk.ordinal)
                )
            )
            .scalars()
            .all()
        )
        if not chunks:
            continue
        # Dimension guard — BEFORE embedding, so a mismatch costs nothing.
        # The `document_chunks.embedding` column is a fixed pgvector size. If
        # the target embedder's known output dimension differs, writing it
        # would fail the flush anyway — skip this source and log loudly rather
        # than pay to discover it. A chunk written but never embedded carries
        # NULL, so measure the column width from a chunk that has a vector and
        # fall back to the column's declared width when none does.
        embedded_dims = [len(c.embedding) for c in chunks if c.embedding is not None]
        existing_dim = embedded_dims[0] if embedded_dims else EMBEDDING_DIM
        mismatch_dim = embedding_dim_mismatch(current_model, column_dim=existing_dim)
        if mismatch_dim is not None:
            # Marked + skipped, never re-billed (F5): the model is NOT called.
            # The record's `embedder_model` is deliberately left at its old
            # value, so it stays in the stale set (`embedder_model != current`)
            # — a durable, queryable "needs re-embed but dim-blocked" mark that
            # the next sweep re-detects and re-skips (still no spend) until the
            # binding is corrected. `dim_blocked` is counted + logged.
            dim_blocked += 1
            rec.state = "lexical_only"
            rec.degraded_reason = (
                f"the bound embedding model '{current_model}' emits "
                f"{mismatch_dim}-dim vectors, the store holds {existing_dim}-dim"
            )
            _log.warning(
                "rks.reembed.dim_mismatch_skipped",
                source_id=str(rec.source_id),
                existing_dim=existing_dim,
                new_dim=mismatch_dim,
                model=current_model,
            )
            continue
        # `embedding_text`, not `c.text`. The repair path used to embed the bare
        # chunk unconditionally, so a corpus ingested under one representation
        # was silently re-embedded under another by the job whose entire purpose
        # is to make the index consistent.
        title = (
            await session.execute(select(Source.title).where(Source.id == rec.source_id))
        ).scalar_one_or_none()
        result = await embed_texts(
            client=client,
            principal=principal,
            project_id=project_id,
            agent_run_id=None,
            profile_bindings=profile_bindings,
            texts=[
                embedding_text(chunk_text=c.text, title=title, section_path=c.section_path)
                for c in chunks
            ],
            purpose=purpose,
        )
        if len(result.embeddings) != len(chunks):
            continue
        for chunk, emb in zip(chunks, result.embeddings, strict=True):
            chunk.embedding = emb
            chunk.embedder_model = index_signature(result.model)
        rec.embedder_model = index_signature(result.model)
        rec.indexed_at = utcnow()
        rec.state = "embedded"
        rec.degraded_reason = None
        await session.flush()
        sources_done += 1
        chunks_done += len(chunks)
    if dim_blocked:
        _log.warning("rks.reembed.dim_blocked_total", project_id=str(project_id), count=dim_blocked)
    return sources_done, chunks_done
