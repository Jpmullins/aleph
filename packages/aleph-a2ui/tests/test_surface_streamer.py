"""Wave 4 T6 — pure JSON-pointer diff for incremental `updateDataModel` deltas.

`diff_data_model(prev, nxt)` returns the minimal list of RFC-6902-ish patch
ops (`replace` / `add` / `remove`) needed to turn `prev` into `nxt`. The
streamer maps each op into a v0_9 `updateDataModel` message (see
`surface_streamer.py` for the `remove` handling note — the wire has no per-leaf
remove primitive).
"""

from __future__ import annotations

from aleph_a2ui.messages import full_surface
from aleph_a2ui.surface_streamer import (
    data_model_patches_to_messages,
    diff_data_model,
    split_surface_messages,
)


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


# ---------------------------------------------------------------------------
# split_surface_messages — separate the structural messages from the root model
# ---------------------------------------------------------------------------


def test_split_extracts_root_data_model() -> None:
    model = {"items": [{"id": "H1"}], "title": "T"}
    messages = full_surface(
        surface_id="hypotheses",
        catalog_id="aleph://v1",
        components=[{"component": "List", "id": "root"}],
        data_model=model,
    )
    # full_surface emits createSurface, updateComponents, updateDataModel(path="/").
    assert len(messages) == 3
    structural, data_model = split_surface_messages(messages)
    # The root updateDataModel is pulled out; only structural messages remain.
    assert data_model == model
    assert len(structural) == 2
    assert "createSurface" in structural[0]
    assert "updateComponents" in structural[1]
    assert all("updateDataModel" not in m for m in structural)


def test_split_no_data_model_yields_empty_dict() -> None:
    messages = full_surface(
        surface_id="wiki",
        catalog_id="aleph://v1",
        components=[{"component": "Text", "id": "root"}],
        data_model=None,
    )
    structural, data_model = split_surface_messages(messages)
    assert data_model == {}
    assert len(structural) == 2


def test_split_keeps_non_root_data_model_messages_structural() -> None:
    # A leaf updateDataModel (non-root path) is NOT the seed model; it must stay
    # in the structural stream rather than be mistaken for the root data model.
    from aleph_a2ui.messages import update_components, update_data_model

    messages = [
        update_components(surface_id="s", components=[{"component": "X", "id": "root"}]),
        update_data_model(surface_id="s", path="/items/0", value={"id": "H1"}),
    ]
    structural, data_model = split_surface_messages(messages)
    assert data_model == {}
    assert len(structural) == 2


# ---------------------------------------------------------------------------
# data_model_patches_to_messages — map diff ops onto v0_9 updateDataModel wire
# ---------------------------------------------------------------------------


def _udm(msg: dict) -> dict:
    """Unwrap an updateDataModel envelope to {path, value, surfaceId}."""
    assert msg["version"] == "v0.9"
    return msg["updateDataModel"]


def test_patches_empty_is_no_messages() -> None:
    assert data_model_patches_to_messages(surface_id="s", patches=[], next_model={}) == []


def test_patches_replace_and_add_map_directly() -> None:
    patches = [
        {"op": "replace", "path": "/items/0/confidence", "value": "high"},
        {"op": "add", "path": "/items/0/title", "value": "T"},
    ]
    msgs = data_model_patches_to_messages(
        surface_id="hyp", patches=patches, next_model={"items": [{}]}
    )
    bodies = [_udm(m) for m in msgs]
    assert {"surfaceId": "hyp", "path": "/items/0/confidence", "value": "high"} in bodies
    assert {"surfaceId": "hyp", "path": "/items/0/title", "value": "T"} in bodies
    assert len(bodies) == 2


def test_patches_object_key_remove_sets_null() -> None:
    # An object-key remove has no per-leaf wire primitive; it is expressed as
    # updateDataModel(value=None) so DataModel.set deletes the key.
    msgs = data_model_patches_to_messages(
        surface_id="s",
        patches=[{"op": "remove", "path": "/items/0/title"}],
        next_model={"items": [{}]},
    )
    assert len(msgs) == 1
    assert _udm(msgs[0]) == {"surfaceId": "s", "path": "/items/0/title", "value": None}


def test_patches_array_remove_resets_whole_array() -> None:
    # Removing an array element falls back to re-setting the whole parent array
    # once (from next_model); any patch inside that array is then skipped.
    next_model = {"items": [{"id": "H1"}]}
    patches = [
        {"op": "replace", "path": "/items/0/x", "value": 9},  # inside /items → covered
        {"op": "remove", "path": "/items/1"},  # array remove → triggers reset
    ]
    msgs = data_model_patches_to_messages(surface_id="s", patches=patches, next_model=next_model)
    assert len(msgs) == 1
    body = _udm(msgs[0])
    assert body["path"] == "/items"
    assert body["value"] == [{"id": "H1"}]


def test_diff_then_messages_roundtrip_for_a_delta() -> None:
    # End-to-end of the pure delta pipeline: a hypothesis's confidence changes
    # and a second hypothesis is appended → two updateDataModel messages.
    prev = {"items": [{"id": "H1", "confidence": "low"}]}
    nxt = {"items": [{"id": "H1", "confidence": "high"}, {"id": "H2", "confidence": "low"}]}
    patches = diff_data_model(prev, nxt)
    msgs = data_model_patches_to_messages(surface_id="hyp", patches=patches, next_model=nxt)
    bodies = [_udm(m) for m in msgs]
    assert {"surfaceId": "hyp", "path": "/items/0/confidence", "value": "high"} in bodies
    assert {
        "surfaceId": "hyp",
        "path": "/items/1",
        "value": {"id": "H2", "confidence": "low"},
    } in bodies
    assert len(bodies) == 2
