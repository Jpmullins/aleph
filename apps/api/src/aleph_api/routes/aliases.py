"""Aliases API: list, add, repair broken wikilinks."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.alias_service import AliasService
from aleph_wiki.models import Alias

router = APIRouter(prefix="/v1/projects", tags=["wiki-aliases"])


class AliasIn(BaseModel):
    surface_form: str = Field(min_length=1, max_length=512)
    canonical_name: str = Field(min_length=1, max_length=512)
    canonical_page_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0, le=1.0)


class AliasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    surface_form: str
    canonical_name: str
    canonical_page_id: UUID | None
    confidence: float
    created_at: datetime


@router.get("/{project_id}/wiki/aliases", response_model=list[AliasOut])
async def list_aliases(
    project_id: ProjectScopeDep,
    session: SessionDep,
    surface_form: Annotated[str | None, Query()] = None,
) -> list[AliasOut]:
    stmt = select(Alias).where(Alias.project_id == project_id).order_by(Alias.surface_form)
    if surface_form:
        stmt = stmt.where(Alias.surface_form == surface_form)
    rows = list((await session.execute(stmt)).scalars().all())
    return [AliasOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/wiki/aliases",
    status_code=status.HTTP_201_CREATED,
    response_model=AliasOut,
)
async def add_alias(
    project_id: ProjectScopeDep,
    body: Annotated[AliasIn, Body()],
    session: SessionDep,
    principal: PrincipalDep,
) -> AliasOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    svc = AliasService(session)
    a = await svc.upsert(
        project_id=project_id,
        surface_form=body.surface_form,
        canonical_name=body.canonical_name,
        canonical_page_id=body.canonical_page_id,
        confidence=body.confidence,
        created_by=principal.user_id,
    )
    return AliasOut.model_validate(a)


@router.post("/{project_id}/wiki/aliases/repair-links")
async def repair_links(
    project_id: ProjectScopeDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, int]:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    svc = AliasService(session)
    n = await svc.repair_broken_links(project_id=project_id)
    return {"repaired": n}
