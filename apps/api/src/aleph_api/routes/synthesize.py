"""Synthesize action + synthesis proposal approval/rejection routes.

The /synthesize endpoint creates an AgentRun(kind="aiq_deep"), issues
an AIQ service token bound to that run, dispatches the job to the AIQ
server, and returns the run id so the chat surface can subscribe to
progress events.

Approve / reject endpoints flip the proposal status and the underlying
WikiPage.status in one transaction, recording an ApprovalDecision row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_aiq.auth_bridge import issue_service_token
from aleph_aiq.client import AIQClient
from aleph_aiq.job_service import append_aiq_event, create_aiq_agent_run
from aleph_connectors.models import ApprovalDecision, SynthesisProposal
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_rks.models import Connector, ConnectorBinding
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.feedback_service import write_feedback
from aleph_wiki.models import WikiPage

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

router = APIRouter(prefix="/v1/projects", tags=["synthesize"])


class SynthesizeIn(BaseModel):
    topic: str = Field(min_length=1, max_length=512)
    depth: str = Field(default="deep", pattern=r"^(shallow|deep)$")
    allowed_connectors: list[str] | None = None  # connector kinds; None = all enabled


class SynthesizeOut(BaseModel):
    agent_run_id: str
    correlation_id: str
    aiq_job_id: str | None
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

    # Resolve allowed connectors from project bindings (enabled=True only).
    stmt = (
        select(Connector.kind)
        .join(ConnectorBinding, ConnectorBinding.connector_id == Connector.id)
        .where(
            ConnectorBinding.project_id == project_id,
            ConnectorBinding.enabled.is_(True),
        )
    )
    enabled = [k for (k,) in (await session.execute(stmt)).all()]
    if body.allowed_connectors:
        enabled = [k for k in enabled if k in body.allowed_connectors]
    if not enabled:
        msg = "no connectors are enabled for this project"
        raise ValidationFailed(msg)

    started = await create_aiq_agent_run(
        session,
        project_id=project_id,
        topic=body.topic,
        depth=body.depth,
        allowed_connector_kinds=enabled,
        created_by=principal.user_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="synthesize.dispatch",
        target_id=started.agent_run_id,
        target_kind="agent_run",
        payload={
            "topic": body.topic,
            "depth": body.depth,
            "allowed_connectors": enabled,
        },
        trace_id=current_trace_id(),
    )

    settings = request.app.state.settings
    service_token = issue_service_token(
        secret=settings.aleph_agent_token_secret,
        project_id=project_id,
        agent_run_id=started.agent_run_id,
        principal_user_id=principal.user_id,
    )

    # Dispatch to the AIQ server. If AIQ is not yet vendored / not reachable,
    # the AgentRun stays in 'pending' and the caller can re-dispatch later.
    aiq_base = getattr(settings, "aiq_base_url", None) or "http://aiq-server:8001"
    aiq_job_id: str | None = None
    dispatched = False
    client = AIQClient(base_url=aiq_base, service_token=service_token)
    try:
        if await client.health():
            aiq_job_id = await client.dispatch_deep(
                project_id=project_id,
                topic=body.topic,
                allowed_data_sources=enabled,
            )
            dispatched = True
            await append_aiq_event(
                session,
                agent_run_id=started.agent_run_id,
                event_kind="aiq.job.dispatched",
                payload={"aiq_job_id": aiq_job_id},
            )
    except Exception as exc:  # noqa: BLE001
        await append_aiq_event(
            session,
            agent_run_id=started.agent_run_id,
            event_kind="aiq.dispatch.failed",
            payload={"error": str(exc)[:1024]},
        )

    return SynthesizeOut(
        agent_run_id=str(started.agent_run_id),
        correlation_id=started.correlation_id,
        aiq_job_id=aiq_job_id,
        dispatched=dispatched,
    )


@router.get(
    "/{project_id}/synthesis-proposals", response_model=list[ProposalOut]
)
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
        access_scope="project",
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
        access_scope="project",
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
