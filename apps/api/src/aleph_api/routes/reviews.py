"""ReviewRun + ReviewFinding + ApprovalRequest API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_core.errors import NotFound
from aleph_reviewer.approval_service import decide
from aleph_reviewer.models import ApprovalRequest, ReviewFinding, ReviewRun
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

router = APIRouter(prefix="/v1/projects", tags=["reviews"])


class ReviewRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    trigger: str
    target_revision_id: UUID | None
    target_scope: str
    status: str
    finding_count: int
    started_at: datetime
    completed_at: datetime | None


class ReviewFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    review_run_id: UUID
    finding_kind: str
    severity: str
    title: str
    description: str
    target_page_id: UUID | None
    target_revision_id: UUID | None
    target_section_anchor: str | None
    target_claim_id: UUID | None
    target_source_id: UUID | None
    evidence_refs_jsonb: list
    proposed_patch_jsonb: dict | None
    auto_resolvable: bool
    status: str
    approval_request_id: UUID | None
    created_at: datetime


class ApprovalRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    target_kind: str
    target_id: UUID
    title: str
    summary: str
    severity: str
    status: str
    decided_at: datetime | None
    decision_id: UUID | None
    created_at: datetime


class DecideIn(BaseModel):
    decision: str = Field(pattern=r"^(approved|rejected)$")
    reason: str | None = Field(default=None, max_length=4096)


@router.get("/{project_id}/reviews/runs", response_model=list[ReviewRunOut])
async def list_runs(
    project_id: ProjectScopeDep,
    session: SessionDep,
    kind: Annotated[str | None, Query()] = None,
) -> list[ReviewRunOut]:
    stmt = (
        select(ReviewRun)
        .where(ReviewRun.project_id == project_id)
        .order_by(ReviewRun.started_at.desc())
    )
    if kind:
        stmt = stmt.where(ReviewRun.kind == kind)
    rows = list((await session.execute(stmt)).scalars().all())
    return [ReviewRunOut.model_validate(r) for r in rows]


@router.get(
    "/{project_id}/reviews/findings", response_model=list[ReviewFindingOut]
)
async def list_findings(
    project_id: ProjectScopeDep,
    session: SessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[ReviewFindingOut]:
    stmt = (
        select(ReviewFinding)
        .where(ReviewFinding.project_id == project_id)
        .order_by(ReviewFinding.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(ReviewFinding.status == status_filter)
    rows = list((await session.execute(stmt)).scalars().all())
    return [ReviewFindingOut.model_validate(r) for r in rows]


@router.get(
    "/{project_id}/approval-requests",
    response_model=list[ApprovalRequestOut],
)
async def list_approval_requests(
    project_id: ProjectScopeDep,
    session: SessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[ApprovalRequestOut]:
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.project_id == project_id)
        .order_by(ApprovalRequest.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    rows = list((await session.execute(stmt)).scalars().all())
    return [ApprovalRequestOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/approval-requests/{request_id}/decide",
    response_model=ApprovalRequestOut,
)
async def decide_approval(
    project_id: ProjectScopeDep,
    request_id: UUID,
    body: Annotated[DecideIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ApprovalRequestOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    req = await decide(
        session,
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        request_id=request_id,
        decision=body.decision,
        reason=body.reason,
    )
    return ApprovalRequestOut.model_validate(req)
