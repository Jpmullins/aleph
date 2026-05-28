"""ApprovalRequest CRUD + decide.

Wraps Inc 3's `ApprovalDecision` with workflow state and target
context. On `decide()`, writes the `ApprovalDecision` row, updates the
target's status (synthesis proposal, review finding, wiki revision,
hypothesis update), and ledgers the decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_connectors.models import ApprovalDecision
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.models import ApprovalRequest, ReviewFinding

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


async def create_request(
    session: "AsyncSession",
    *,
    project_id: UUID,
    target_kind: str,
    target_id: UUID,
    title: str,
    summary: str,
    severity: str,
    proposed_patch: dict[str, Any] | None,
    evidence_refs: list[dict[str, Any]],
    requested_by: "Principal",
) -> ApprovalRequest:
    req = ApprovalRequest(
        id=uuid7(),
        project_id=project_id,
        target_kind=target_kind,
        target_id=target_id,
        title=title[:255],
        summary=summary,
        severity=severity,
        proposed_patch_jsonb=proposed_patch,
        evidence_refs_jsonb=evidence_refs,
        requested_by_kind=requested_by.actor_kind,
        requested_by_id=requested_by.user_id,
        status="pending",
        created_by=requested_by.user_id,
        access_scope="project",
    )
    session.add(req)
    await session.flush()
    return req


async def decide(
    session: "AsyncSession",
    *,
    ledger: "LedgerWriter",
    principal: "Principal",
    project_id: UUID,
    request_id: UUID,
    decision: str,
    reason: str | None,
) -> ApprovalRequest:
    if decision not in ("approved", "rejected"):
        msg = f"invalid decision: {decision}"
        raise ValidationFailed(msg)
    req = (
        await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.id == request_id,
                ApprovalRequest.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if req is None:
        msg = f"approval request {request_id} not found"
        raise NotFound(msg)
    if req.status != "pending":
        msg = f"approval already {req.status}"
        raise ValidationFailed(msg)

    decision_row = ApprovalDecision(
        id=uuid7(),
        project_id=project_id,
        target_kind=req.target_kind,
        target_id=req.target_id,
        decision=decision,
        reason=reason,
        decided_by=principal.user_id,
        decided_at=utcnow(),
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(decision_row)
    req.status = decision
    req.decided_at = utcnow()
    req.decision_id = decision_row.id

    # Mirror onto the linked ReviewFinding if applicable.
    if req.target_kind == "review_finding":
        finding = await session.get(ReviewFinding, req.target_id)
        if finding is not None:
            finding.status = decision

    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind=f"approval_request.{decision}",
        target_id=req.id,
        target_kind="approval_request",
        payload={
            "target_kind": req.target_kind,
            "target_id": str(req.target_id),
            "reason": reason,
        },
        trace_id=current_trace_id(),
    )
    return req
