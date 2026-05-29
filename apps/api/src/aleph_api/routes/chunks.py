"""Chunks API: list + intra-source search (used by Inc 2 descent)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from aleph_api.deps import LiteLLMDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound, ValidationFailed
from aleph_db.repos import model_profile as profile_repo
from aleph_rks.models import DocumentChunk, Source
from aleph_rks.retrieval import descend_into_source

router = APIRouter(prefix="/v1/projects", tags=["chunks"])


class ChunkOut(BaseModel):
    id: UUID
    ordinal: int
    section_path: str | None
    text: str
    token_count: int


@router.get("/{project_id}/sources/{source_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(
    project_id: ProjectScopeDep,
    source_id: UUID,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ChunkOut]:
    src = (
        await session.execute(
            select(Source).where(Source.id == source_id, Source.project_id == project_id)
        )
    ).scalar_one_or_none()
    if src is None:
        msg = f"source not found: {source_id}"
        raise NotFound(msg)
    stmt = (
        select(DocumentChunk)
        .where(DocumentChunk.source_id == source_id)
        .order_by(DocumentChunk.ordinal)
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        ChunkOut(
            id=c.id,
            ordinal=c.ordinal,
            section_path=c.section_path,
            text=c.text,
            token_count=c.token_count,
        )
        for c in rows
    ]


class ChunkSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    top_k: int = Field(default=8, ge=1, le=50)


class ChunkSearchHitOut(BaseModel):
    chunk_id: UUID
    ordinal: int
    section_path: str | None
    text: str
    score: float


@router.post(
    "/{project_id}/sources/{source_id}/chunks/search",
    response_model=list[ChunkSearchHitOut],
)
async def search_chunks(
    project_id: ProjectScopeDep,
    source_id: UUID,
    body: Annotated[ChunkSearchIn, Body()],
    session: SessionDep,
    litellm: LiteLLMDep,
    principal: PrincipalDep,
) -> list[ChunkSearchHitOut]:
    src = (
        await session.execute(
            select(Source).where(Source.id == source_id, Source.project_id == project_id)
        )
    ).scalar_one_or_none()
    if src is None:
        msg = f"source not found: {source_id}"
        raise NotFound(msg)
    profile = await profile_repo.get_project_profile(session, project_id)
    if profile is None:
        msg = "project has no model profile"
        raise ValidationFailed(msg)
    # Embed the query (one batch).
    resp = await litellm.embed(
        principal=principal,
        project_id=project_id,
        agent_run_id=None,
        profile_bindings=profile.bindings_jsonb,
        input=[body.query],
        purpose="rks.descent.query_embed",
    )
    if not resp.embeddings:
        return []
    hits = await descend_into_source(
        session,
        project_id=project_id,
        source_id=source_id,
        query_text=body.query,
        query_embedding=resp.embeddings[0],
        top_k=body.top_k,
    )
    return [
        ChunkSearchHitOut(
            chunk_id=h.chunk_id,
            ordinal=h.ordinal,
            section_path=h.section_path,
            text=h.text,
            score=h.score,
        )
        for h in hits
    ]
