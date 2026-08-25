"""Wave 4 T2 — v0_9 server-to-client message builders.

Wire shape verified against
`apps/web/node_modules/@a2ui/web_core/src/v0_9/schema/server-to-client.d.ts`
and the working `apps/web/src/a2ui/aleph-catalog-v09.tsx`: each message is a
*nested envelope* `{ "version": "v0.9", "<kind>": { ... } }`, NOT a flat
`{ "kind": ... }` object. The MessageProcessor discriminates on the presence of
the `createSurface` / `updateComponents` / `updateDataModel` key.
"""

from __future__ import annotations

from aleph_a2ui.components.surfaces import (
    ALEPH_V09_CATALOG_ID,
    artifacts_surface_v09,
    briefs_surface_v09,
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


def _assert_bound_surface(
    msgs: list[dict], *, surface_id: str, component: str
) -> tuple[dict, dict]:
    """A data-bound tab = full_surface: createSurface + updateComponents (one
    root component whose props are `{path}` bindings) + a root updateDataModel.
    Returns (root component, data model)."""
    assert len(msgs) == 3
    assert list(msgs[0].keys()) == ["version", "createSurface"]
    assert msgs[0]["createSurface"]["surfaceId"] == surface_id
    assert msgs[0]["createSurface"]["catalogId"] == ALEPH_V09_CATALOG_ID
    comps = msgs[1]["updateComponents"]["components"]
    assert len(comps) == 1
    assert comps[0]["id"] == "root"
    assert comps[0]["component"] == component
    assert msgs[2]["updateDataModel"]["path"] == "/"
    return comps[0], msgs[2]["updateDataModel"]["value"]


def test_wiki_surface_v09_binds_pages_and_open() -> None:
    comp, model = _assert_bound_surface(
        wiki_surface_v09(pages=[{"id": "p1", "title": "T"}], open_page=None),
        surface_id="wiki",
        component="WikiSurface",
    )
    assert comp["pages"] == {"path": "/pages"}
    assert comp["open"] == {"path": "/open"}
    assert model == {
        "pages": [{"id": "p1", "title": "T"}],
        "open": None,
        # Present and empty rather than absent. The client binder resolves only
        # the paths it is given, and a path missing from the data model resolves
        # to something that is not a list — which is how the wiki rendered with
        # no categories at all while the server was sending ten.
        "categories": [],
        "health": {},
    }


def test_wiki_surface_v09_binds_categories_and_health() -> None:
    """Every prop the producer sends needs a binding, or it never arrives.

    The binder resolves ONLY declared bindings. A prop added to the data model
    without a matching `{"path": ...}` on the component is dropped silently —
    the payload is correct, the view sees `undefined`, and nothing reports an
    error. That is the write-path-with-no-read-path failure this codebase keeps
    producing, so it is pinned here on both halves.
    """
    cats = [{"id": "logging-recovery", "title": "Logging and Recovery", "blurb": "WAL"}]
    health = {"pages_scanned": 25, "by_severity": {"broken": 280}}
    comp, model = _assert_bound_surface(
        wiki_surface_v09(pages=[], open_page=None, categories=cats, health=health),
        surface_id="wiki",
        component="WikiSurface",
    )
    assert comp["categories"] == {"path": "/categories"}
    assert comp["health"] == {"path": "/health"}
    assert model["categories"] == cats
    assert model["health"] == health


def test_artifacts_surface_v09_binds_sources_and_artifacts() -> None:
    comp, model = _assert_bound_surface(
        artifacts_surface_v09(sources=[{"id": "s1"}], artifacts=[]),
        surface_id="library",
        component="ArtifactsSurface",
    )
    assert comp["sources"] == {"path": "/sources"}
    assert comp["artifacts"] == {"path": "/artifacts"}
    assert model == {"sources": [{"id": "s1"}], "artifacts": []}


def test_notes_surface_v09_binds_notes() -> None:
    comp, model = _assert_bound_surface(
        notes_surface_v09(notes=[{"id": "n1", "title": "N", "body_md": "b"}]),
        surface_id="notes",
        component="NotesSurface",
    )
    assert comp["notes"] == {"path": "/notes"}
    assert model["notes"][0]["id"] == "n1"


def test_briefs_surface_v09_carries_approval_children() -> None:
    cards = [{"type": "ApprovalCard", "id": "synth-1", "props": {"title": "x"}}]
    comp = _assert_surface_messages(
        briefs_surface_v09(badge_count=1, children=cards),
        surface_id="briefs",
        component="BriefsSurface",
    )
    assert comp["badge_count"] == 1
    assert comp["children"] == cards
