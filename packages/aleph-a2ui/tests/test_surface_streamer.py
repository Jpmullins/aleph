"""Wave 4 T6 — pure JSON-pointer diff for incremental `updateDataModel` deltas.

`diff_data_model(prev, nxt)` returns the minimal list of RFC-6902-ish patch
ops (`replace` / `add` / `remove`) needed to turn `prev` into `nxt`. The
streamer maps each op into a v0_9 `updateDataModel` message (see
`surface_streamer.py` for the `remove` handling note — the wire has no per-leaf
remove primitive).
"""

from __future__ import annotations

from aleph_a2ui.surface_streamer import diff_data_model


def test_replace_scalar() -> None:
    assert diff_data_model(
        {"items": [{"id": "H1", "confidence": "low"}]},
        {"items": [{"id": "H1", "confidence": "high"}]},
    ) == [{"op": "replace", "path": "/items/0/confidence", "value": "high"}]


def test_append_item() -> None:
    p = diff_data_model({"items": []}, {"items": [{"id": "H2"}]})
    assert len(p) == 1 and p[0]["op"] == "add" and p[0]["path"].startswith("/items/")


def test_no_change() -> None:
    assert diff_data_model({"items": [{"id": "H1"}]}, {"items": [{"id": "H1"}]}) == []


def test_remove_item() -> None:
    p = diff_data_model({"items": [{"id": "H1"}, {"id": "H2"}]}, {"items": [{"id": "H1"}]})
    assert p == [{"op": "remove", "path": "/items/1"}]


def test_nested_replace_and_add() -> None:
    p = diff_data_model(
        {"items": [{"id": "H1", "confidence": "low", "evidence_count": 0}]},
        {"items": [{"id": "H1", "confidence": "high", "evidence_count": 2}]},
    )
    assert {"op": "replace", "path": "/items/0/confidence", "value": "high"} in p
    assert {"op": "replace", "path": "/items/0/evidence_count", "value": 2} in p
    assert len(p) == 2


def test_add_object_key() -> None:
    p = diff_data_model({"items": [{"id": "H1"}]}, {"items": [{"id": "H1", "title": "T"}]})
    assert p == [{"op": "add", "path": "/items/0/title", "value": "T"}]


def test_remove_object_key() -> None:
    p = diff_data_model({"items": [{"id": "H1", "title": "T"}]}, {"items": [{"id": "H1"}]})
    assert p == [{"op": "remove", "path": "/items/0/title"}]


def test_pointer_escaping() -> None:
    # JSON-pointer requires ~1 for "/" and ~0 for "~" in a key.
    p = diff_data_model({"a/b": 1}, {"a/b": 2})
    assert p == [{"op": "replace", "path": "/a~1b", "value": 2}]


def test_type_change_is_replace() -> None:
    p = diff_data_model({"x": {"a": 1}}, {"x": [1, 2]})
    assert p == [{"op": "replace", "path": "/x", "value": [1, 2]}]


def test_scalar_value_change() -> None:
    p = diff_data_model({"x": 1}, {"x": 1})
    assert p == []
