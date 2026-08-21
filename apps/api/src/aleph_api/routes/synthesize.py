"""Synthesize action + synthesis proposal approval/rejection routes.

The /synthesize endpoint creates an AgentRun (agent_kind
"deep_research" | "shallow_research"), writes the synthesize.dispatch
ledger event in the same transaction, mints an agent token bound to
that run, and enqueues the native ``deep_research_job`` — the run id is
returned so the chat surface can subscribe to progress events.

Approve / reject endpoints flip the proposal status and the underlying
WikiPage.status in one transaction, recording an ApprovalDecision row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_connectors.models import ApprovalDecision, SynthesisProposal
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_research.dispatch import dispatch_research
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.feedback_service import write_feedback
from aleph_wiki.models import WikiPage

router = APIRouter(prefix="/v1/projects", tags=["synthesize"])


class SynthesizeIn(BaseModel):
    topic: str = Field(min_length=1, max_length=512)
    depth: str = Field(default="deep", pattern=r"^(shallow|deep)$")
    allowed_connectors: list[str] | None = None  # connector kinds; None = all enabled


class SynthesizeOut(BaseModel):
    agent_run_id: str
    correlation_id: str
    dispatched: bool


class ProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    page_id: UUID
    revision_id: UUID
    agent_run_id: UUID
    topic: str
    status: str
    approval_decision_id: UUID | None
    created_at: datetime


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4096)


@router.post(
    "/{project_id}/synthesize",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SynthesizeOut,
)
async def synthesize(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[SynthesizeIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> SynthesizeOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)

    # Resolve enabled connector kinds → create the pending AgentRun + ledger
    # the dispatch → enqueue the native deep_research_job, via the shared
    # helper (the same path the bootstrap_project_job uses).
    settings = request.app.state.settings
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        try:
            started = await dispatch_research(
                session=session,
                ledger=ledger,
                redis_pool=pool,
                agent_token_secret=settings.aleph_agent_token_secret,
                project_id=project_id,
                principal_user_id=principal.user_id,
                actor_kind=principal.actor_kind,
                topic=body.topic,
                depth=body.depth,
                allowed_connectors=body.allowed_connectors,
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
    finally:
        await pool.aclose()

    return SynthesizeOut(
        agent_run_id=str(started.agent_run_id),
        correlation_id=started.correlation_id,
        dispatched=started.dispatched,
    )


@router.get("/{project_id}/synthesis-proposals", response_model=list[ProposalOut])
async def list_proposals(
    project_id: ProjectScopeDep,
    session: SessionDep,
    status_filter: Annotated[str | None, None] = None,
) -> list[ProposalOut]:
    stmt = (
        select(SynthesisProposal)
        .where(SynthesisProposal.project_id == project_id)
        .order_by(SynthesisProposal.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(SynthesisProposal.status == status_filter)
    rows = list((await session.execute(stmt)).scalars().all())
    return [ProposalOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/synthesis-proposals/{proposal_id}/approve",
    response_model=ProposalOut,
)
async def approve(
    project_id: ProjectScopeDep,
    proposal_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ProposalOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = (
        await session.execute(
            select(SynthesisProposal).where(
                SynthesisProposal.id == proposal_id,
                SynthesisProposal.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        msg = f"proposal {proposal_id} not found"
        raise NotFound(msg)
    if p.status != "pending":
        msg = f"proposal already {p.status}"
        raise ValidationFailed(msg)
    decision = ApprovalDecision(
        id=uuid7(),
        project_id=project_id,
        target_kind="synthesis_proposal",
        target_id=proposal_id,
        decision="approved",
        reason=None,
        decided_by=principal.user_id,
        decided_at=utcnow(),
        created_by=principal.user_id,
    )
    session.add(decision)
    p.status = "approved"
    p.approval_decision_id = decision.id

    page = await session.get(WikiPage, p.page_id)
    if page is not None:
        page.status = "approved"
    await session.flush()

    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="synthesis.proposal.approve",
        target_id=proposal_id,
        target_kind="synthesis_proposal",
        payload={"page_id": str(p.page_id), "revision_id": str(p.revision_id)},
        trace_id=current_trace_id(),
    )
    return ProposalOut.model_validate(p)


@router.post(
    "/{project_id}/synthesis-proposals/{proposal_id}/reject",
    response_model=ProposalOut,
)
async def reject(
    project_id: ProjectScopeDep,
    proposal_id: UUID,
    body: Annotated[RejectIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ProposalOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = (
        await session.execute(
            select(SynthesisProposal).where(
                SynthesisProposal.id == proposal_id,
                SynthesisProposal.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        msg = f"proposal {proposal_id} not found"
        raise NotFound(msg)
    if p.status != "pending":
        msg = f"proposal already {p.status}"
        raise ValidationFailed(msg)
    decision = ApprovalDecision(
        id=uuid7(),
        project_id=project_id,
        target_kind="synthesis_proposal",
        target_id=proposal_id,
        decision="rejected",
        reason=body.reason,
        decided_by=principal.user_id,
        decided_at=utcnow(),
        created_by=principal.user_id,
    )
    session.add(decision)
    p.status = "rejected"
    p.approval_decision_id = decision.id

    page = await session.get(WikiPage, p.page_id)
    if page is not None:
        page.status = "archived"  # soft-delete; data preserved
    await session.flush()

    await write_feedback(
        session,
        project_id=project_id,
        page_id=p.page_id,
        concept_name=p.topic,
        rejected_revision_id=p.revision_id,
        reason=body.reason,
        rejected_by=principal.user_id,
    )

    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="synthesis.proposal.reject",
        target_id=proposal_id,
        target_kind="synthesis_proposal",
        payload={
            "page_id": str(p.page_id),
            "revision_id": str(p.revision_id),
            "reason": body.reason,
        },
        trace_id=current_trace_id(),
    )
    return ProposalOut.model_validate(p)
