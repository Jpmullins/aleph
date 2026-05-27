"""Hash-chain computation (pure-function-level test)."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from aleph_db.repos.ledger import _canonical_json, _compute_chain_hash


def test_canonical_json_is_sorted() -> None:
    a = _canonical_json({"b": 1, "a": 2})
    b = _canonical_json({"a": 2, "b": 1})
    assert a == b
    assert a == '{"a":2,"b":1}'


def test_chain_hash_deterministic() -> None:
    target_id = uuid4()
    args = {
        "prev_hash": "0" * 64,
        "action_kind": "project.create",
        "target_id": target_id,
        "payload": {"title": "x"},
        "timestamp_iso": "2026-05-27T12:00:00+00:00",
    }
    h1 = _compute_chain_hash(**args)
    h2 = _compute_chain_hash(**args)
    assert h1 == h2
    assert len(h1) == 64
    # Change one input, hash changes.
    h3 = _compute_chain_hash(**{**args, "action_kind": "project.update"})
    assert h3 != h1


def test_chain_hash_matches_explicit_sha256() -> None:
    prev = "0" * 64
    payload = {"x": 1}
    ts = "2026-05-27T12:00:00+00:00"
    target = uuid4()
    h = hashlib.sha256()
    h.update(prev.encode())
    h.update(b"|")
    h.update(b"a.b")
    h.update(b"|")
    h.update(str(target).encode())
    h.update(b"|")
    h.update(_canonical_json(payload).encode())
    h.update(b"|")
    h.update(ts.encode())
    expected = h.hexdigest()
    got = _compute_chain_hash(
        prev_hash=prev,
        action_kind="a.b",
        target_id=target,
        payload=payload,
        timestamp_iso=ts,
    )
    assert got == expected
