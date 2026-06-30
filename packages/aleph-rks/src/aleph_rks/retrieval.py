"""Intra-source descent — pgvector cosine + FTS hybrid within a single Source.

Used by the wiki retrieval router (Inc 2) when a wiki answer cites
`[[Source:X]]` and the composer needs more detail from that one source.
We do NOT search across sources here — embeddings are intentionally
intra-source-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import func, select

from aleph_rks.models import DocumentChunk, RetrievalIndexRecord

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


async def descend_into_source(
    session: AsyncSession,
    *,
    project_id: UUID,
    source_id: UUID,
    query_text: str,
    query_embedding: list[float],
    top_k: int = 8,
) -> list[ChunkHit]:
    """Hybrid: top-k by cosine (vector) blended with FTS rank.

    Score = 0.6 * cosine_similarity + 0.4 * fts_rank (both [0,1]-normalized).
    """
    cosine_stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.ordinal,
            DocumentChunk.text,
            DocumentChunk.section_path,
            (
                1.0 - func.cast(DocumentChunk.embedding.cosine_distance(query_embedding), float)
            ).label(  # type: ignore[attr-defined]
                "cos_sim"
            ),
        )
        .where(
            DocumentChunk.project_id == project_id,
            DocumentChunk.source_id == source_id,
        )
        .order_by(DocumentChunk.embedding.cosine_distance(query_embedding))  # type: ignore[attr-defined]
        .limit(top_k * 4)
    )
    cosine_rows = list((await session.execute(cosine_stmt)).all())

    fts_stmt = select(
        DocumentChunk.id,
        func.ts_rank(
            DocumentChunk.text_tsv,
            func.plainto_tsquery("english", query_text),
        ).label("rank"),
    ).where(
        DocumentChunk.project_id == project_id,
        DocumentChunk.source_id == source_id,
        DocumentChunk.text_tsv.op("@@")(func.plainto_tsquery("english", query_text)),
    )
    fts_rows = {row.id: float(row.rank) for row in (await session.execute(fts_stmt)).all()}
    if fts_rows:
        fts_max = max(fts_rows.values()) or 1.0
        fts_rows = {k: v / fts_max for k, v in fts_rows.items()}

    hits: list[ChunkHit] = []
    for cid, ordinal, txt, path, cos_sim in cosine_rows:
        fts = fts_rows.get(cid, 0.0)
        score = 0.6 * float(cos_sim) + 0.4 * float(fts)
        hits.append(
            ChunkHit(
                chunk_id=cid,
                ordinal=ordinal,
                text=txt,
                section_path=path,
                score=score,
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


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
    from aleph_rks.embedding import embed_texts

    current_model = resolve_binding(profile_bindings, "embedding").model
    stale = list(
        (
            await session.execute(
                select(RetrievalIndexRecord).where(
                    RetrievalIndexRecord.project_id == project_id,
                    RetrievalIndexRecord.embedder_model != current_model,
                )
            )
        )
        .scalars()
        .all()
    )
    sources_done = 0
    chunks_done = 0
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
        # Dimension guard: the `document_chunks.embedding` column is a fixed
        # pgvector size (1024 for the default titan-embed-v2). If the new
        # embedder emits a different dimension, writing it would fail the whole
        # flush — skip this source and log loudly rather than abort the job.
        existing_dim = len(chunks[0].embedding)
        new_dim = len(result.embeddings[0]) if result.embeddings else existing_dim
        if new_dim != existing_dim:
            _log.warning(
                "rks.reembed.dim_mismatch_skipped",
                source_id=str(rec.source_id),
                existing_dim=existing_dim,
                new_dim=new_dim,
                model=result.model,
            )
            continue
        for chunk, emb in zip(chunks, result.embeddings, strict=True):
            chunk.embedding = emb
            chunk.embedder_model = result.model
        rec.embedder_model = result.model
        rec.indexed_at = utcnow()
        await session.flush()
        sources_done += 1
        chunks_done += len(chunks)
    return sources_done, chunks_done
