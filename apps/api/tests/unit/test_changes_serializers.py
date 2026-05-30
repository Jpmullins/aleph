"""Unit tests for the pure change-signal serializers (`routes/changes.py`).

Pure ORM-rows → list[dict] mappers; no DB. We construct ORM instances with only
the attributes the serializers read (SQLAlchemy allows session-less construction).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aleph_api.routes.changes import ledger_rows_to_signals, phase_rows_to_signals
from aleph_db.models.agent import AgentEvent
from aleph_db.models.ledger import ActionLedgerEvent

_TS = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


def _ledger(action_kind: str, *, target_id=None, payload=None, ts=_TS) -> ActionLedgerEvent:
    return ActionLedgerEvent(
        action_kind=action_kind,
        target_id=target_id,
        actor_kind="aleph_agent",
        payload_jsonb=payload or {},
        timestamp=ts,
    )


def _event(event_kind: str, *, payload=None, ts=_TS) -> AgentEvent:
    return AgentEvent(
        event_kind=event_kind,
        payload_jsonb=payload or {},
        timestamp=ts,
    )


def test_ledger_commit_maps_to_committed_signal() -> None:
    pid = uuid.uuid4()
    rows = [_ledger("wiki.revision.commit", target_id=pid, payload={"page_title": "Acme"})]
    sigs = ledger_rows_to_signals(rows)
    assert sigs == [
        {
            "kind": "committed",
            "action_kind": "wiki.revision.commit",
            "page_id": str(pid),
            "page_title": "Acme",
            "actor_kind": "aleph_agent",
            "ts": _TS.isoformat(),
        }
    ]


def test_ledger_non_allowlisted_kind_dropped() -> None:
    rows = [_ledger("hypothesis.create", target_id=uuid.uuid4())]
    assert ledger_rows_to_signals(rows) == []


def test_ledger_empty() -> None:
    assert ledger_rows_to_signals([]) == []


def test_phase_started_maps_to_compiling() -> None:
    sigs = phase_rows_to_signals(
        [
            _event(
                "phase_started",
                payload={"phase": "compile_page", "page_title": "Acme", "page_kind": "source"},
            )
        ]
    )
    assert sigs == [
        {
            "kind": "compiling",
            "page_id": None,
            "page_title": "Acme",
            "ts": _TS.isoformat(),
            "page_kind": "source",
        }
    ]


def test_phase_completed_maps_to_compile_done_with_page_id() -> None:
    pid = str(uuid.uuid4())
    sigs = phase_rows_to_signals(
        [
            _event(
                "phase_completed",
                payload={"phase": "compile_page", "page_id": pid, "page_title": "Acme"},
            )
        ]
    )
    assert sigs == [
        {"kind": "compile_done", "page_id": pid, "page_title": "Acme", "ts": _TS.isoformat()}
    ]


def test_phase_non_compile_page_dropped() -> None:
    # A normal workflow phase (not compile_page) is not a presence signal.
    assert (
        phase_rows_to_signals([_event("phase_started", payload={"phase": "concept_extraction"})])
        == []
    )


def test_phase_without_any_page_identity_dropped() -> None:
    assert phase_rows_to_signals([_event("phase_started", payload={"phase": "compile_page"})]) == []
