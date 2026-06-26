"""Page-merge proposal approval/rejection routes (wiki curator).

The curator proposes a merge when it detects a near-duplicate page; merging is
the only curator action gated by a human. Approve runs
``CuratorService.apply_merge`` (redirect inbound links source→target, alias the
source title to the target, soft-delete the source); reject leaves both pages.
Both record a generic ``ApprovalDecision``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_connectors.models import ApprovalDecision
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.curator_service import CuratorService
from aleph_wiki.models import PageMergeProposal

router = APIRouter(prefix="/v1/projects", tags=["merge-proposals"])


class MergeProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    source_page_id: UUID
    target_page_id: UUID
    rationale: str
    similarity: float
    status: str
    approval_decision_id: UUID | None
    created_at: datetime


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4096)


async def _load_pending(
    session: SessionDep, project_id: UUID, proposal_id: UUID
) -> PageMergeProposal:
    p = (
        await session.execute(
            select(PageMergeProposal).where(
                PageMergeProposal.id == proposal_id,
                PageMergeProposal.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        msg = f"merge proposal {proposal_id} not found"
        raise NotFound(msg)
    if p.status != "pending":
        msg = f"merge proposal already {p.status}"
        raise ValidationFailed(msg)
    return p


@router.get("/{project_id}/merge-proposals", response_model=list[MergeProposalOut])
async def list_merge_proposals(
    project_id: ProjectScopeDep,
    session: SessionDep,
    status_filter: Annotated[str | None, None] = None,
) -> list[MergeProposalOut]:
    stmt = (
        select(PageMergeProposal)
        .where(PageMergeProposal.project_id == project_id)
        .order_by(PageMergeProposal.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(PageMergeProposal.status == status_filter)
    rows = list((await session.execute(stmt)).scalars().all())
    return [MergeProposalOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/merge-proposals/{proposal_id}/approve",
    response_model=MergeProposalOut,
)
async def approve_merge(
    project_id: ProjectScopeDep,
    proposal_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> MergeProposalOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = await _load_pending(session, project_id, proposal_id)
    decision = ApprovalDecision(
        id=uuid7(),
        project_id=project_id,
        target_kind="page_merge_proposal",
        target_id=proposal_id,
        decision="approved",
        reason=None,
        decided_by=principal.user_id,
        decided_at=utcnow(),
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(decision)
    # apply_merge redirects links, aliases the source, soft-deletes it, and
    # appends its own wiki.page.merge ledger event.
    await CuratorService(session).apply_merge(proposal=p, principal=principal, ledger=ledger)
    p.status = "approved"
    p.approval_decision_id = decision.id
    await session.flush()
    return MergeProposalOut.model_validate(p)


@router.post(
    "/{project_id}/merge-proposals/{proposal_id}/reject",
    response_model=MergeProposalOut,
)
async def reject_merge(
    project_id: ProjectScopeDep,
    proposal_id: UUID,
    body: Annotated[RejectIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> MergeProposalOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = await _load_pending(session, project_id, proposal_id)
    decision = ApprovalDecision(
        id=uuid7(),
        project_id=project_id,
        target_kind="page_merge_proposal",
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
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="wiki.page.merge.reject",
        target_id=proposal_id,
        target_kind="page_merge_proposal",
        payload={"reason": body.reason},
        trace_id=current_trace_id(),
    )
    return MergeProposalOut.model_validate(p)
