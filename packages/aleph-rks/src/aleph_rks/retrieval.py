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
from sqlalchemy import func, or_, select

from aleph_core.rrf import fuse
from aleph_core.tsquery import or_tsquery
from aleph_rks.models import EMBEDDING_DIM, DocumentChunk, RetrievalIndexRecord

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: UUID
    ordinal: int
    text: str
    section_path: str | None
    score: float
    source_id: UUID | None = None


#: How many chunks any one source may contribute to a corpus-wide result.
#: Without a cap, a single long document with many near-identical chunks fills
#: the whole window and the answer sees one source's view of the question.
DEFAULT_PER_SOURCE_CAP = 3


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
    scope = [DocumentChunk.project_id == project_id]
    if source_id is not None:
        scope.append(DocumentChunk.source_id == source_id)

    fetch = max(top_k * 4, 40)

    dense_rows: list[Any] = []
    # `embedding IS NOT NULL` is load-bearing, not defensive. A chunk is written
    # before it is embedded, so an un-embedded chunk is a normal row — and
    # ordering by `cosine_distance` over a NULL vector is undefined in
    # Postgres, not simply last. Without this predicate a degraded index makes
    # the dense leg return an arbitrary page of rows, which RRF then fuses as
    # though it were a ranking. An empty dense leg is the honest answer;
    # `search_corpus` still answers from the lexical leg, which needs no model.
    if query_embedding is not None:
        dense_stmt = (
            select(
                DocumentChunk.id,
                DocumentChunk.ordinal,
                DocumentChunk.text,
                DocumentChunk.section_path,
                DocumentChunk.source_id,
            )
            .where(*scope, DocumentChunk.embedding.isnot(None))
            .order_by(DocumentChunk.embedding.cosine_distance(query_embedding))  # type: ignore[attr-defined]
            .limit(fetch)
        )
        dense_rows = list((await session.execute(dense_stmt)).all())

    # OR the terms. Every Postgres parser conjoins unquoted words, so a whole
    # question would otherwise have to match every content word to return
    # anything. `or_tsquery` widens the candidate set; ts_rank still orders a
    # full match above a partial one. See aleph_core.tsquery.
    tsquery = or_tsquery(query_text)
    lexical_stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.ordinal,
            DocumentChunk.text,
            DocumentChunk.section_path,
            DocumentChunk.source_id,
        )
        .where(*scope, DocumentChunk.text_tsv.op("@@")(tsquery))
        .order_by(func.ts_rank(DocumentChunk.text_tsv, tsquery).desc())
        .limit(fetch)
    )
    lexical_rows = list((await session.execute(lexical_stmt)).all())

    by_id = {row.id: row for row in [*dense_rows, *lexical_rows]}
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
            )
        )
        if len(hits) >= top_k:
            break
    return hits


async def search_corpus(
    session: AsyncSession,
    *,
    project_id: UUID,
    query_text: str,
    query_embedding: list[float] | None,
    top_k: int = 8,
    per_source_cap: int | None = DEFAULT_PER_SOURCE_CAP,
) -> list[ChunkHit]:
    """Search every source in a project. This is first-line retrieval.

    ``query_embedding=None`` searches lexically only — the honest degraded mode
    when no embedder is available.
    """
    return await _hybrid_search(
        session,
        project_id=project_id,
        query_text=query_text,
        query_embedding=query_embedding,
        top_k=top_k,
        source_id=None,
        per_source_cap=per_source_cap,
    )


async def descend_into_source(
    session: AsyncSession,
    *,
    project_id: UUID,
    source_id: UUID,
    query_text: str,
    query_embedding: list[float] | None,
    top_k: int = 8,
) -> list[ChunkHit]:
    """The same search, pinned to one source. No per-source cap: there is one."""
    return await _hybrid_search(
        session,
        project_id=project_id,
        query_text=query_text,
        query_embedding=query_embedding,
        top_k=top_k,
        source_id=source_id,
        per_source_cap=None,
    )


async def get_index_record(session: AsyncSession, source_id: UUID) -> RetrievalIndexRecord | None:
    stmt = select(RetrievalIndexRecord).where(RetrievalIndexRecord.source_id == source_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def needs_reembed(
    session: AsyncSession, source_id: UUID, *, current_embedder_model: str
) -> bool:
    rec = await get_index_record(session, source_id)
    if rec is None:
        return True
    return rec.embedder_model != current_embedder_model


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
    from aleph_core.time import utcnow
    from aleph_models.profile import resolve_binding
    from aleph_rks.embedding import embed_texts, embedding_dim_mismatch

    current_model = resolve_binding(profile_bindings, "embedding").model
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
        result = await embed_texts(
            client=client,
            principal=principal,
            project_id=project_id,
            agent_run_id=None,
            profile_bindings=profile_bindings,
            texts=[c.text for c in chunks],
            purpose=purpose,
        )
        if len(result.embeddings) != len(chunks):
            continue
        for chunk, emb in zip(chunks, result.embeddings, strict=True):
            chunk.embedding = emb
            chunk.embedder_model = result.model
        rec.embedder_model = result.model
        rec.indexed_at = utcnow()
        rec.state = "embedded"
        rec.degraded_reason = None
        await session.flush()
        sources_done += 1
        chunks_done += len(chunks)
    if dim_blocked:
        _log.warning("rks.reembed.dim_blocked_total", project_id=str(project_id), count=dim_blocked)
    return sources_done, chunks_done
