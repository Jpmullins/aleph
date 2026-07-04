"""Shared UserFeedback writer.

The user-feedback mutation (insert + ledger + lazy eval-case promotion) is
reached from two entry points that must behave identically:

* the REST endpoint `POST /v1/projects/{id}/feedback` (`routes/evals.py`), and
* the `feedback` A2UI card action (`a2ui_handlers.py`) — WP-4 routed the surface
  cards' feedback button through the ledger-audited action router instead of a
  component-level `useMutation`.

Factoring it here keeps the single write path (and its eval promotion) from
drifting between the two callers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.feedback import UserFeedback
from aleph_core.ids import uuid7
from aleph_evals.models import EvalCase, EvalDataset
from aleph_observability.tracing import current_trace_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter

# Signals strong enough to seed a regression case.
_PROMOTE_SIGNALS = ("marked_wrong", "misleading", "false_positive")


async def record_feedback(
    session: AsyncSession,
    ledger: LedgerWriter,
    *,
    project_id: UUID,
    actor_id: UUID,
    actor_kind: str,
    target_kind: str,
    target_id: UUID,
    signal: str,
    note: str = "",
    severity: str | None = None,
    context: dict[str, Any] | None = None,
) -> UserFeedback:
    """Insert a `UserFeedback` row, write its ledger event, and (for high-signal
    feedback) promote it to a regression `EvalCase`. Returns the row."""
    fb = UserFeedback(
        id=uuid7(),
        project_id=project_id,
        target_kind=target_kind,
        target_id=target_id,
        signal=signal,
        note=note,
        severity=severity,
        context_jsonb=context or {},
        promoted_to_eval_case_id=None,
        created_by=actor_id,
        access_scope="project",
    )
    session.add(fb)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        action_kind=f"feedback.{signal}",
        target_id=target_id,
        target_kind=target_kind,
        payload={"severity": severity, "note_length": len(note)},
        trace_id=current_trace_id(),
    )
    if signal in _PROMOTE_SIGNALS:
        await _promote_to_eval_case(session, fb)
    return fb


async def _promote_to_eval_case(session: AsyncSession, fb: UserFeedback) -> None:
    dataset_name = f"user_feedback:{fb.project_id}"
    existing = (
        await session.execute(select(EvalDataset).where(EvalDataset.name == dataset_name))
    ).scalar_one_or_none()
    if existing is None:
        existing = EvalDataset(
            id=uuid7(),
            name=dataset_name,
            description=f"User feedback promoted to regression cases for {fb.project_id}",
            kind="metric_only",
            case_count=0,
            fixture_path=f"<runtime:user_feedback:{fb.project_id}>",
            gate_kind="warning",
            gate_thresholds_jsonb={},
            introduced_in_increment=8,
            created_by=fb.created_by,
            access_scope="project",
        )
        session.add(existing)
        await session.flush()
    case = EvalCase(
        id=uuid7(),
        eval_dataset_id=existing.id,
        case_key=f"feedback-{fb.id}",
        payload_jsonb={
            "target_kind": fb.target_kind,
            "target_id": str(fb.target_id),
            "context": fb.context_jsonb,
        },
        expected_jsonb={
            "signal_not_in": list(_PROMOTE_SIGNALS),
            "note": fb.note,
        },
        tags_jsonb=["user_feedback", fb.signal],
        origin="user_feedback",
        origin_ref_id=fb.id,
    )
    session.add(case)
    existing.case_count += 1
    fb.promoted_to_eval_case_id = case.id
    await session.flush()
