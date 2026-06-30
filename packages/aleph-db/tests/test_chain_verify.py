"""verify_event_chain — pure recompute over hand-built events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from aleph_db.repos.ledger import _compute_chain_hash, verify_event_chain


@dataclass
class _Link:
    id: object
    action_kind: str
    target_id: object
    payload_jsonb: dict
    timestamp: datetime
    chain_hash: str


def _build_chain(n: int) -> list[_Link]:
    links: list[_Link] = []
    prev = "0" * 64
    for i in range(n):
        ts = datetime(2026, 6, 30, 12, 0, i, tzinfo=timezone.utc)
        tid = uuid4()
        payload = {"i": i}
        ch = _compute_chain_hash(
            prev_hash=prev,
            action_kind="test.event",
            target_id=tid,
            payload=payload,
            timestamp_iso=ts.isoformat(),
        )
        links.append(_Link(uuid4(), "test.event", tid, payload, ts, ch))
        prev = ch
    return links


def test_intact_chain_verifies_ok() -> None:
    result = verify_event_chain(_build_chain(3))
    assert result.ok is True
    assert result.count == 3
    assert result.first_divergence is None


def test_tampered_chain_reports_first_divergence() -> None:
    links = _build_chain(3)
    # Simulate a tampered payload: the stored chain_hash no longer matches.
    links[1].payload_jsonb = {"i": 999}
    result = verify_event_chain(links)
    assert result.ok is False
    assert result.first_divergence is not None
    assert result.first_divergence.event_id == links[1].id


def test_empty_chain_is_ok() -> None:
    result = verify_event_chain([])
    assert result.ok is True
    assert result.count == 0
