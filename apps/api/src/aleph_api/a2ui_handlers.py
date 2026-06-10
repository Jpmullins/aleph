"""ActionRouter handler registrations.

Each handler executes the requested action inside the dispatching
session + ledger. Handlers receive the same kwargs `ActionRouter`
collects (session, ledger, principal, project_id, request) and return
a dict that ends up in `CardAction.result_jsonb`.

This module is imported once at app lifespan startup; the registrations
populate `app.state.action_router`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_a2ui.action_router import ActionRouter, CardActionRequest
from aleph_a2ui.card_service import unpin_card
from aleph_connectors.models import (
    ApprovalDecision,
    SynthesisProposal,
)
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_notes.note_service import update_section as update_note_section
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.models import ApprovalRequest, ReviewFinding
from aleph_wiki.feedback_service import write_feedback
from aleph_wiki.handedit_service import clear_section, mark_section
from aleph_wiki.models import WikiPage

# Agent actions an approval may execute on approve. The approve handler
# dispatches via this fixed map (route + how to shape the request body), never
# via arbitrary agent input — args were persisted server-side at request time.
_AGENT_ACTION_ROUTES: dict[str, str] = {
    "build_artifact": "/v1/projects/{project_id}/artifacts/build",
    "set_connector_enabled": "/v1/projects/{project_id}/connectors/bindings",
}

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _execute_agent_action(
    *, project_id: UUID, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Execute an approved agent action by self-calling its underlying route.

    Dispatch is keyed by `_AGENT_ACTION_ROUTES` (a fixed allowlist); `tool` is
    validated by the caller. `args` were persisted server-side when the approval
    was requested, so this is not agent-emitted-executable. The real route writes
    its own ledger + state in its own transaction.
    """
    import httpx

    from aleph_api.settings import get_settings

    route = _AGENT_ACTION_ROUTES[tool].format(project_id=project_id)
    base = get_settings().aleph_self_url
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base}{route}",
            json=args,
            # local-auth-mode only (consistent with the other agent tools). Under
            # OIDC this self-call would need a real short-lived agent token.
            headers={"Authorization": "Bearer local-dev"},
        )
    if resp.status_code >= 400:
        msg = f"executing {tool} failed ({resp.status_code}): {resp.text[:200]}"
        raise ValidationFailed(msg)
    return resp.json()


async def _approve(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
) -> dict[str, Any]:
    target_id = UUID(request.params["target_id"])
    target_kind = request.params["target_kind"]
    if target_kind == "synthesis_proposal":
        p = (
            await session.execute(
                select(SynthesisProposal).where(
                    SynthesisProposal.id == target_id,
                    SynthesisProposal.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if p is None:
            msg = f"proposal {target_id} not found"
            raise NotFound(msg)
        if p.status != "pending":
            msg = f"proposal already {p.status}"
            raise ValidationFailed(msg)
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="synthesis_proposal",
            target_id=target_id,
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
        return {"target": str(target_id), "new_status": "approved"}
    if target_kind == "agent_action":
        # Row-level lock: two concurrent approves of the same agent action must
        # serialize at the DB level. With FOR UPDATE the second txn blocks until
        # the first commits, then sees status != pending and no-ops below — so the
        # multi-second effect (build / connector toggle) executes exactly once.
        req = (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.target_id == target_id,
                    ApprovalRequest.target_kind == "agent_action",
                    ApprovalRequest.project_id == project_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if req is None:
            msg = f"agent-action approval {target_id} not found"
            raise NotFound(msg)
        if req.status != "pending":
            # Already decided (e.g. a concurrent approve committed first).
            # No-op — do NOT re-execute the effect.
            return {"target": str(target_id), "new_status": req.status, "noop": True}
        patch: dict[str, Any] = req.proposed_patch_jsonb or {}
        tool = str(patch.get("tool") or "")
        args: dict[str, Any] = patch.get("args") or {}
        if tool not in _AGENT_ACTION_ROUTES:
            msg = f"agent action tool {tool!r} is not in the execution allowlist"
            raise ValidationFailed(msg)
        # Execute the real effect (self-call the underlying route) BEFORE marking
        # approved, so a failed execution leaves the request pending.
        executed = await _execute_agent_action(project_id=project_id, tool=tool, args=args)
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="agent_action",
            target_id=target_id,
            decision="approved",
            reason=None,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        req.status = "approved"
        req.decided_at = utcnow()
        req.decision_id = decision.id
        await session.flush()
        # Mirror approval_service.decide(): write the specific decision ledger
        # event (rule #4), in addition to the generic a2ui.action.approve event
        # the ActionRouter appends.
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="approval_request.approved",
            target_id=req.id,
            target_kind="approval_request",
            payload={"tool": tool, "target_kind": "agent_action"},
            trace_id=current_trace_id(),
        )
        return {
            "target": str(target_id),
            "new_status": "approved",
            "executed": executed,
        }
    if target_kind == "review_finding":
        finding = (
            await session.execute(
                select(ReviewFinding).where(
                    ReviewFinding.id == target_id,
                    ReviewFinding.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if finding is None:
            msg = f"finding {target_id} not found"
            raise NotFound(msg)
        if finding.status != "open":
            msg = f"finding already {finding.status}"
            raise ValidationFailed(msg)
        finding.status = "resolved"
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="review_finding.resolve",
            target_id=finding.id,
            target_kind="review_finding",
            payload={"title": finding.title, "finding_kind": finding.finding_kind},
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "resolved"}
    msg = f"approve handler not wired for target_kind={target_kind}"
    raise ValidationFailed(msg)


async def _reject(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
) -> dict[str, Any]:
    target_id = UUID(request.params["target_id"])
    target_kind = request.params["target_kind"]
    reason = request.params["reason"]
    if target_kind == "synthesis_proposal":
        p = (
            await session.execute(
                select(SynthesisProposal).where(
                    SynthesisProposal.id == target_id,
                    SynthesisProposal.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if p is None:
            msg = f"proposal {target_id} not found"
            raise NotFound(msg)
        if p.status != "pending":
            msg = f"proposal already {p.status}"
            raise ValidationFailed(msg)
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="synthesis_proposal",
            target_id=target_id,
            decision="rejected",
            reason=reason,
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
            page.status = "archived"
        await session.flush()
        await write_feedback(
            session,
            project_id=project_id,
            page_id=p.page_id,
            concept_name=p.topic,
            rejected_revision_id=p.revision_id,
            reason=reason,
            rejected_by=principal.user_id,
        )
        return {"target": str(target_id), "new_status": "rejected"}
    if target_kind == "agent_action":
        # Row-level lock for consistency with _approve — serialize concurrent
        # decisions on the same agent action at the DB level.
        req = (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.target_id == target_id,
                    ApprovalRequest.target_kind == "agent_action",
                    ApprovalRequest.project_id == project_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if req is None:
            msg = f"agent-action approval {target_id} not found"
            raise NotFound(msg)
        if req.status != "pending":
            # Already decided by a concurrent decision — no-op.
            return {"target": str(target_id), "new_status": req.status, "noop": True}
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="agent_action",
            target_id=target_id,
            decision="rejected",
            reason=reason,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        req.status = "rejected"
        req.decided_at = utcnow()
        req.decision_id = decision.id
        # No execution on reject.
        await session.flush()
        # Mirror approval_service.decide(): write the specific decision ledger
        # event (rule #4), in addition to the generic a2ui.action.reject event.
        patch: dict[str, Any] = req.proposed_patch_jsonb or {}
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="approval_request.rejected",
            target_id=req.id,
            target_kind="approval_request",
            payload={"tool": str(patch.get("tool") or ""), "target_kind": "agent_action"},
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "rejected"}
    if target_kind == "review_finding":
        finding = (
            await session.execute(
                select(ReviewFinding).where(
                    ReviewFinding.id == target_id,
                    ReviewFinding.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if finding is None:
            msg = f"finding {target_id} not found"
            raise NotFound(msg)
        if finding.status != "open":
            msg = f"finding already {finding.status}"
            raise ValidationFailed(msg)
        finding.status = "dismissed"
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="review_finding.dismiss",
            target_id=finding.id,
            target_kind="review_finding",
            payload={
                "title": finding.title,
                "finding_kind": finding.finding_kind,
                "reason": reason,
            },
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "dismissed"}
    msg = f"reject handler not wired for target_kind={target_kind}"
    raise ValidationFailed(msg)


async def _unpin(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
) -> dict[str, Any]:
    card_id = UUID(request.params["card_id"])
    card = await unpin_card(session, project_id=project_id, card_id=card_id)
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="card.unpin",
        target_id=card.id,
        target_kind="interactive_card",
        payload={"card_kind": card.card_kind, "title": card.title or ""},
        trace_id=current_trace_id(),
    )
    return {"card_id": str(card_id), "pinned_to": None}


async def _open(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    return {
        "navigate": {
            "target_id": request.params["target_id"],
            "target_kind": request.params["target_kind"],
        }
    }


async def _navigate_wiki(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    return {"page_id": request.params["page_id"]}


async def _edit_note(
    *,
    session: AsyncSession,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    section_id = UUID(request.params["section_id"])
    body_md = request.params["body_md"]
    s = await update_note_section(
        session,
        project_id=project_id,
        section_id=section_id,
        body_md=body_md,
    )
    return {"section_id": str(s.id), "ordinal": s.ordinal}


async def _mark_handedit(
    *,
    session: AsyncSession,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    page_id = UUID(request.params["page_id"])
    anchor = request.params["section_anchor"]
    m = await mark_section(
        session,
        project_id=project_id,
        page_id=page_id,
        section_anchor=anchor,
        applied_by=principal.user_id,
    )
    return {"mark_id": str(m.id)}


async def _clear_handedit(
    *,
    session: AsyncSession,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    page_id = UUID(request.params["page_id"])
    anchor = request.params["section_anchor"]
    n = await clear_section(
        session,
        project_id=project_id,
        page_id=page_id,
        section_anchor=anchor,
        cleared_by=principal.user_id,
    )
    return {"cleared": n}


async def _submit_form(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    return {
        "form_id": request.params["form_id"],
        "values": request.params["values"],
    }


async def _create_hypothesis(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    # Inc 5 wires this for real. For now return a clear signal that it's pending.
    return {"pending": "create_hypothesis lands in Increment 5"}


async def _clarify(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    # Inc 3 wires the AIQ clarifier loop; this handler is the surface end.
    return {
        "agent_run_id": request.params["agent_run_id"],
        "answer_length": len(request.params["answer"]),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def build_action_router() -> ActionRouter:
    r = ActionRouter()
    r.register("approve", _approve)
    r.register("reject", _reject)
    r.register("open", _open)
    r.register("unpin", _unpin)
    r.register("navigate_wiki", _navigate_wiki)
    r.register("submit_form", _submit_form)
    r.register("create_hypothesis", _create_hypothesis)
    r.register("edit_note", _edit_note)
    r.register("clarify", _clarify)
    r.register("mark_handedit", _mark_handedit)
    r.register("clear_handedit", _clear_handedit)
    return r
