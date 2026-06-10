"""Wave 4 T2 — v0_9 server-to-client message builders.

Wire shape verified against
`apps/web/node_modules/@a2ui/web_core/src/v0_9/schema/server-to-client.d.ts`
and the working `apps/web/src/a2ui/_spike/SpikePanel.tsx`: each message is a
*nested envelope* `{ "version": "v0.9", "<kind>": { ... } }`, NOT a flat
`{ "kind": ... }` object. The MessageProcessor discriminates on the presence of
the `createSurface` / `updateComponents` / `updateDataModel` key.
"""

from __future__ import annotations

from aleph_a2ui.components.surfaces import (
    ALEPH_V09_CATALOG_ID,
    artifacts_surface_v09,
    briefs_surface_v09,
    hypotheses_surface_v09,
    hypothesis_cards_v09,
    notes_surface_v09,
    wiki_surface_v09,
)
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


# ---------------------------------------------------------------------------
# Wave 4 T3 — every tab's v0.9 surface builder is well-formed.
# ---------------------------------------------------------------------------


def _assert_surface_messages(msgs: list[dict], *, surface_id: str, component: str) -> dict:
    """A surface message list = createSurface then updateComponents with one
    top-level component bound to the shared catalog. Returns that component.

    The single component MUST carry id "root": upstream `<A2uiSurface>` renders
    only the component whose id is "root" (else `[Loading root...]`)."""
    assert len(msgs) == 2
    assert list(msgs[0].keys()) == ["version", "createSurface"]
    assert msgs[0]["createSurface"]["surfaceId"] == surface_id
    assert msgs[0]["createSurface"]["catalogId"] == ALEPH_V09_CATALOG_ID
    assert list(msgs[1].keys()) == ["version", "updateComponents"]
    assert msgs[1]["updateComponents"]["surfaceId"] == surface_id
    comps = msgs[1]["updateComponents"]["components"]
    assert len(comps) == 1
    assert comps[0]["component"] == component
    assert comps[0]["id"] == "root"
    return comps[0]


def test_wiki_surface_v09_shape() -> None:
    comp = _assert_surface_messages(
        wiki_surface_v09(children=[]), surface_id="wiki", component="WikiSurface"
    )
    assert comp["view_mode"] == "page"
    assert comp["children"] == []


def test_artifacts_surface_v09_shape() -> None:
    _assert_surface_messages(
        artifacts_surface_v09(), surface_id="artifacts", component="ArtifactsSurface"
    )


def test_notes_surface_v09_shape() -> None:
    cards = [{"type": "NotebookCellCard", "id": "s-1", "props": {"section_id": "x"}}]
    comp = _assert_surface_messages(
        notes_surface_v09(children=cards), surface_id="notes", component="NotesSurface"
    )
    assert comp["children"] == cards


def test_hypotheses_surface_v09_shape() -> None:
    _assert_surface_messages(
        hypotheses_surface_v09(), surface_id="hypotheses", component="HypothesesSurface"
    )


def test_hypothesis_cards_v09_shape() -> None:
    msgs = hypothesis_cards_v09(
        hypotheses=[
            {"hypothesis_id": "h1", "title": "T", "confidence": "likely", "evidence_count": 2}
        ]
    )
    # full_surface list: createSurface + updateComponents + root updateDataModel.
    assert list(msgs[0].keys()) == ["version", "createSurface"]
    assert msgs[0]["createSurface"]["surfaceId"] == "hypotheses"
    comps = msgs[1]["updateComponents"]["components"]
    assert comps[0]["component"] == "Column"
    assert comps[1]["component"] == "HypothesisCard"
    assert comps[1]["confidence"] == {"path": "/items/0/confidence"}
    assert msgs[2]["updateDataModel"]["value"]["items"][0]["evidence_count"] == 2


def test_briefs_surface_v09_carries_approval_children() -> None:
    cards = [{"type": "ApprovalCard", "id": "synth-1", "props": {"title": "x"}}]
    comp = _assert_surface_messages(
        briefs_surface_v09(badge_count=1, children=cards),
        surface_id="briefs",
        component="BriefsSurface",
    )
    assert comp["badge_count"] == 1
    assert comp["children"] == cards
