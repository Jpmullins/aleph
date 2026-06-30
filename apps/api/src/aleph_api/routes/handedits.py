"""Hand-edit mark / clear endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from aleph_api.deps import PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.handedit_service import clear_section, mark_section

router = APIRouter(prefix="/v1/projects", tags=["wiki-handedits"])


class HandEditMarkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    page_id: UUID
    section_anchor: str | None
    body_sha256_at_edit: str
    applied_at: datetime
    applied_by: UUID
    cleared_at: datetime | None


@router.post(
    "/{project_id}/wiki/pages/{page_id}/sections/{anchor}/handedit",
    status_code=status.HTTP_201_CREATED,
    response_model=HandEditMarkOut,
)
async def mark_handedit(
    project_id: ProjectScopeDep,
    page_id: UUID,
    anchor: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> HandEditMarkOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    m = await mark_section(
        session,
        project_id=project_id,
        page_id=page_id,
        section_anchor=anchor,
        applied_by=principal.user_id,
        ledger=LedgerWriter(session),
        actor_kind=principal.actor_kind,
    )
    return HandEditMarkOut.model_validate(m)


@router.delete(
    "/{project_id}/wiki/pages/{page_id}/sections/{anchor}/handedit",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_handedit(
    project_id: ProjectScopeDep,
    page_id: UUID,
    anchor: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> None:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    await clear_section(
        session,
        project_id=project_id,
        page_id=page_id,
        section_anchor=anchor,
        cleared_by=principal.user_id,
        ledger=LedgerWriter(session),
        actor_kind=principal.actor_kind,
    )
