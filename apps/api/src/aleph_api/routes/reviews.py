"""ReviewRun + ReviewFinding + ApprovalRequest API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.ids import uuid7
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.approval_service import decide
from aleph_reviewer.models import ApprovalRequest, ReviewFinding, ReviewRun
from aleph_security.agent_token import mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

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


class StartEditorialReviewIn(BaseModel):
    trigger: str = Field(default="manual", max_length=512)


class StartEditorialReviewOut(BaseModel):
    enqueued: bool
    trigger: str


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


@router.post(
    "/{project_id}/reviews/editorial",
    response_model=StartEditorialReviewOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_editorial_review(
    project_id: ProjectScopeDep,
    body: Annotated[StartEditorialReviewIn, Body()],
    request: Request,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> StartEditorialReviewOut:
    """Start the editorial reviewer over the project wiki.

    The editorial reviewer scans the project's pages for contradictions,
    weak/missing sources, and coverage gaps, landing ReviewFindings (and, for
    consequential ones, ApprovalRequests). The `trigger` is a free-text label
    carrying the analyst's intent (e.g. the page they asked to review); it is
    recorded on the ReviewRun and surfaced in the Reviews flow. Runs in the
    background via the editorial_review_job worker (rule #3 — never touches the
    DB/worker directly; the worker owns the ReviewRun + findings).
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)

    agent_run_id = uuid7()
    correlation_id = f"editor-{agent_run_id.hex}"
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="review.editorial.start",
        target_id=agent_run_id,
        target_kind="agent_run",
        payload={"trigger": body.trigger},
        trace_id=current_trace_id(),
    )

    token = mint_agent_token(
        secret=request.app.state.settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )
    # No commit needed here: this route writes only a ledger event, and the
    # worker creates the ReviewRun itself rather than reading anything written
    # above. There is no row for it to race against.
    from arq import create_pool
    from arq.connections import RedisSettings

    pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
    try:
        await pool.enqueue_job(
            "editorial_review_job",
            str(project_id),
            body.trigger,
            token,
        )
    finally:
        await pool.aclose()
    return StartEditorialReviewOut(enqueued=True, trigger=body.trigger)


@router.get("/{project_id}/reviews/findings", response_model=list[ReviewFindingOut])
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
