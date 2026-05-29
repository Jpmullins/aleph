"""Intra-source descent — pgvector cosine + FTS hybrid within a single Source.

Used by the wiki retrieval router (Inc 2) when a wiki answer cites
`[[Source:X]]` and the composer needs more detail from that one source.
We do NOT search across sources here — embeddings are intentionally
intra-source-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from aleph_rks.models import DocumentChunk, RetrievalIndexRecord

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
