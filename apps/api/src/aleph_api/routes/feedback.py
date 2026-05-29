"""RejectionFeedback API."""

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
from aleph_wiki.feedback_service import pending_for_concept, write_feedback
from aleph_wiki.models import RejectionFeedback

router = APIRouter(prefix="/v1/projects", tags=["wiki-feedback"])


class RejectionFeedbackIn(BaseModel):
    concept_name: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=4096)
    page_id: UUID | None = None
    rejected_revision_id: UUID | None = None


class RejectionFeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    concept_name: str
    reason: str
    page_id: UUID | None
    rejected_revision_id: UUID | None
    rejected_by: UUID
    rejected_at: datetime
    addressed_in_revision_id: UUID | None


@router.post(
    "/{project_id}/wiki/feedback/rejection",
    status_code=status.HTTP_201_CREATED,
    response_model=RejectionFeedbackOut,
)
async def post_feedback(
    project_id: ProjectScopeDep,
    body: Annotated[RejectionFeedbackIn, Body()],
    session: SessionDep,
    principal: PrincipalDep,
) -> RejectionFeedbackOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    fb = await write_feedback(
        session,
        project_id=project_id,
        page_id=body.page_id,
        concept_name=body.concept_name,
        rejected_revision_id=body.rejected_revision_id,
        reason=body.reason,
        rejected_by=principal.user_id,
    )
    return RejectionFeedbackOut.model_validate(fb)


@router.get(
    "/{project_id}/wiki/feedback/rejection",
    response_model=list[RejectionFeedbackOut],
)
async def list_feedback(
    project_id: ProjectScopeDep,
    session: SessionDep,
    concept_name: Annotated[str | None, Query()] = None,
) -> list[RejectionFeedbackOut]:
    if concept_name:
        rows = await pending_for_concept(session, project_id=project_id, concept_name=concept_name)
        return [RejectionFeedbackOut.model_validate(r) for r in rows]
    stmt = (
        select(RejectionFeedback)
        .where(RejectionFeedback.project_id == project_id)
        .order_by(RejectionFeedback.rejected_at.desc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [RejectionFeedbackOut.model_validate(r) for r in rows]
