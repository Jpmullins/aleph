"""Wave 4 T2 — v0_9 server-to-client message builders.

Wire shape verified against
`apps/web/node_modules/@a2ui/web_core/src/v0_9/schema/server-to-client.d.ts`
and the working `apps/web/src/a2ui/_spike/SpikePanel.tsx`: each message is a
*nested envelope* `{ "version": "v0.9", "<kind>": { ... } }`, NOT a flat
`{ "kind": ... }` object. The MessageProcessor discriminates on the presence of
the `createSurface` / `updateComponents` / `updateDataModel` key.
"""

from __future__ import annotations

from aleph_a2ui.messages import (
    create_surface,
    full_surface,
    update_components,
    update_data_model,
)


def test_create_surface_shape() -> None:
    m = create_surface(surface_id="hypotheses", catalog_id="aleph://v1")
    assert m["version"] == "v0.9"
    assert m["createSurface"]["surfaceId"] == "hypotheses"
    assert m["createSurface"]["catalogId"] == "aleph://v1"


def test_update_components_inlines_props() -> None:
    m = update_components(
        surface_id="hypotheses",
        components=[
            {
                "id": "h1",
                "component": "HypothesisCard",
                "title": "T",
                "confidence": {"path": "/items/0/confidence"},
            }
        ],
    )
    assert m["version"] == "v0.9"
    comp = m["updateComponents"]["components"][0]
    assert comp["component"] == "HypothesisCard"
    # Props (including data bindings) live INLINE on the component object.
    assert comp["confidence"] == {"path": "/items/0/confidence"}
    assert comp["title"] == "T"


def test_update_data_model_shape() -> None:
    m = update_data_model(surface_id="hypotheses", path="/items/0/confidence", value="likely")
    assert m["version"] == "v0.9"
    assert m["updateDataModel"]["surfaceId"] == "hypotheses"
    assert m["updateDataModel"]["path"] == "/items/0/confidence"
    assert m["updateDataModel"]["value"] == "likely"


def test_full_surface_ordered_list() -> None:
    msgs = full_surface(
        surface_id="hypotheses",
        catalog_id="aleph://v1",
        components=[
            {"id": "root", "component": "Column", "children": ["c0"]},
            {
                "id": "c0",
                "component": "HypothesisCard",
                "title": {"path": "/items/0/title"},
                "confidence": {"path": "/items/0/confidence"},
            },
        ],
        data_model={"items": [{"title": "T", "confidence": "likely"}]},
    )
    # Ordered: createSurface first, updateComponents second, then a single
    # bulk root updateDataModel (the wire supports a "/" path with a full value).
    assert list(msgs[0].keys()) == ["version", "createSurface"]
    assert list(msgs[1].keys()) == ["version", "updateComponents"]
    assert msgs[2]["updateDataModel"]["path"] == "/"
    assert msgs[2]["updateDataModel"]["value"] == {
        "items": [{"title": "T", "confidence": "likely"}]
    }
    assert len(msgs) == 3
