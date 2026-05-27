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
from aleph_connectors.models import (
    ApprovalDecision,
    SynthesisProposal,
)
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_notes.note_service import update_section as update_note_section
from aleph_wiki.feedback_service import write_feedback
from aleph_wiki.handedit_service import clear_section, mark_section
from aleph_wiki.models import WikiPage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _approve(
    *,
    session: "AsyncSession",
    ledger: "LedgerWriter",
    principal: "Principal",
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
    msg = f"approve handler not wired for target_kind={target_kind}"
    raise ValidationFailed(msg)


async def _reject(
    *,
    session: "AsyncSession",
    ledger: "LedgerWriter",
    principal: "Principal",
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
    msg = f"reject handler not wired for target_kind={target_kind}"
    raise ValidationFailed(msg)


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
    session: "AsyncSession",
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
    session: "AsyncSession",
    principal: "Principal",
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
    session: "AsyncSession",
    principal: "Principal",
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
    r.register("navigate_wiki", _navigate_wiki)
    r.register("submit_form", _submit_form)
    r.register("create_hypothesis", _create_hypothesis)
    r.register("edit_note", _edit_note)
    r.register("clarify", _clarify)
    r.register("mark_handedit", _mark_handedit)
    r.register("clear_handedit", _clear_handedit)
    return r
