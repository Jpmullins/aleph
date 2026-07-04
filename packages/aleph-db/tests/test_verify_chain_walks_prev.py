"""verify_project_chain walks prev_event_id, not timestamps (WP-5 A2).

A chain whose rows carry out-of-order timestamps but valid `prev_event_id`
links must still verify True; a tampered `chain_hash` or a broken link must
verify False. These are unit tests over a fake session (no compose stack).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from aleph_db.repos.ledger import _compute_chain_hash, verify_project_chain

PROJECT_ID = uuid4()


def _build_linked_chain(n: int, *, timestamps: list[datetime]) -> list[Any]:
    """Build n events linked A→B→C… via prev_event_id, hashed in chain order.

    `timestamps[i]` is stamped on event i regardless of chain position, so the
    caller can deliberately reorder timestamps while keeping links valid.
    """
    events: list[Any] = []
    prev_hash = "0" * 64
    prev_id = None
    for i in range(n):
        ev_id = uuid4()
        tid = uuid4()
        payload = {"i": i}
        ts = timestamps[i]
        ch = _compute_chain_hash(
            prev_hash=prev_hash,
            action_kind="test.event",
            target_id=tid,
            payload=payload,
            timestamp_iso=ts.isoformat(),
        )
        events.append(
            SimpleNamespace(
                id=ev_id,
                prev_event_id=prev_id,
                action_kind="test.event",
                target_id=tid,
                payload_jsonb=payload,
                timestamp=ts,
                chain_hash=ch,
            )
        )
        prev_hash = ch
        prev_id = ev_id
    return events


def _fake_session(rows: list[Any], head_event_id: Any) -> Any:
    head = SimpleNamespace(head_event_id=head_event_id) if head_event_id is not None else None
    # execute() is called twice: events query, then chain-head query.
    payloads = [rows, head]

    class _Result:
        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def scalars(self) -> Any:
            return SimpleNamespace(all=lambda: self._payload)

        def scalar_one_or_none(self) -> Any:
            return self._payload

    class _Session:
        async def execute(self, _stmt: object) -> _Result:
            return _Result(payloads.pop(0))

    return _Session()


async def test_out_of_order_timestamps_but_valid_links_verify_true() -> None:
    # Timestamps deliberately reversed: chain order A→B→C, times C<B<A.
    ts = [
        datetime(2026, 6, 30, 12, 0, 30, tzinfo=UTC),
        datetime(2026, 6, 30, 12, 0, 20, tzinfo=UTC),
        datetime(2026, 6, 30, 12, 0, 10, tzinfo=UTC),
    ]
    events = _build_linked_chain(3, timestamps=ts)
    # Hand rows in a shuffled order — traversal must rely on links, not order.
    shuffled = [events[2], events[0], events[1]]
    session = _fake_session(shuffled, head_event_id=events[-1].id)

    result = await verify_project_chain(session, PROJECT_ID)

    assert result.ok is True
    assert result.count == 3


async def test_valid_links_verify_true_without_head_row() -> None:
    # No LedgerChainHead row: the tip is inferred from the links themselves.
    ts = [datetime(2026, 6, 30, 12, 0, i, tzinfo=UTC) for i in (5, 1, 9)]
    events = _build_linked_chain(3, timestamps=ts)
    session = _fake_session(list(reversed(events)), head_event_id=None)

    result = await verify_project_chain(session, PROJECT_ID)

    assert result.ok is True
    assert result.count == 3


async def test_tampered_chain_hash_verifies_false() -> None:
    ts = [datetime(2026, 6, 30, 12, 0, i, tzinfo=UTC) for i in range(3)]
    events = _build_linked_chain(3, timestamps=ts)
    events[1].chain_hash = "deadbeef" * 8  # tamper the middle hash
    session = _fake_session(list(events), head_event_id=events[-1].id)

    result = await verify_project_chain(session, PROJECT_ID)

    assert result.ok is False
    assert result.first_divergence is not None
    assert result.first_divergence.event_id == events[1].id


async def test_broken_prev_link_verifies_false() -> None:
    ts = [datetime(2026, 6, 30, 12, 0, i, tzinfo=UTC) for i in range(3)]
    events = _build_linked_chain(3, timestamps=ts)
    events[1].prev_event_id = uuid4()  # dangling predecessor
    session = _fake_session(list(events), head_event_id=events[-1].id)

    result = await verify_project_chain(session, PROJECT_ID)

    assert result.ok is False
