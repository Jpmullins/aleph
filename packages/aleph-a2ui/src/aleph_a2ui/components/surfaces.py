"""Surface builders — typed wrappers that emit catalog-conformant payloads."""

from __future__ import annotations

from typing import Any

from aleph_a2ui.messages import create_surface, full_surface, update_components

# Catalog id of the shared v0.9 frontend catalog
# (`apps/web/src/a2ui/aleph-catalog-v09.tsx`). `createSurface.catalogId` must
# reference this exact value for the renderer to resolve component impls.
ALEPH_V09_CATALOG_ID = "aleph://v1"


# ---------------------------------------------------------------------------
# v0.9 message-list builders (Wave 4 T3)
# ---------------------------------------------------------------------------
#
# Each right-panel tab is rendered through the upstream `@a2ui` v0_9
# `MessageProcessor` + `<A2uiSurface>` against the shared catalog (`aleph://v1`).
# The four canonical tabs (Wiki/Library/Notes/Hypotheses) are DATA-BOUND (see
# the builders below); `BriefsSurface` (agent-composed, WP-4d) still rides the
# single-component `_surface_messages` shell with an inline `children` card list
# (the frontend `adapt` helper forwards it to `component.children`).


def _surface_messages(
    *,
    surface_id: str,
    component_name: str,
    props: dict[str, Any] | None = None,
    children: list[dict[str, Any]] | None = None,
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Wrap a single Aleph surface view as a v0.9 message list.

    The component carries its props inline. Legacy card children (Briefs
    `ApprovalCard`s, Wiki embeds) are forwarded as a structural `children` prop;
    the existing views render them via their own renderer.

    The single surface component MUST carry `id="root"`: the upstream
    `@a2ui/react` `<A2uiSurface>` renders exactly the component whose id is
    `"root"` (`DeferredChild id="root"`), falling back to `[Loading root...]`
    when no such component exists. (`hypothesis_cards_v09` satisfies this via
    its root `Column`; these single-component surfaces satisfy it by naming the
    surface view itself `root`.) `surface_id` remains the surface-level id used
    by `createSurface`/`updateComponents` and as the React key.
    """
    component: dict[str, Any] = {"id": "root", "component": component_name}
    if props:
        component.update(props)
    if children is not None:
        component["children"] = children
    return [
        create_surface(surface_id=surface_id, catalog_id=catalog_id),
        update_components(surface_id=surface_id, components=[component]),
    ]


# ---------------------------------------------------------------------------
# Data-bound canonical tab builders (WP-4 sub-spec (a)).
#
# Each of the four canonical tabs (Wiki / Library / Notes / Hypotheses) is now
# SERVER-BUILT and DATA-BOUND: the builder loads its rows (in the route layer,
# which owns the session), then emits a `full_surface` — `createSurface` +
# `updateComponents` (structure, once) + a root `updateDataModel` (the typed
# data model). The single surface component carries its data as `{"path": ...}`
# BINDINGS into that model (the `hypothesis_cards_v09` exemplar pattern), so the
# React view renders ONLY from bound props (zero client fetch) and a mutation
# patches in place via a per-path `updateDataModel` delta (`diff_data_model`) —
# never a full re-render. The self-fetching react-query views are gone
# (`scripts/check-no-self-fetch.sh` enforces it).
# ---------------------------------------------------------------------------


def wiki_surface_v09(
    *,
    pages: list[dict[str, Any]],
    open_page: dict[str, Any] | None = None,
    surface_id: str = "wiki",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Data-bound Wiki tab. Data model: ``{pages: [...], open: {...} | null}``.

    `pages` is the page-browser list; `open` is the currently-open page's reader
    payload (revision body, claims, citations, wikilinks) or ``None`` when
    browsing the index. Opening a page is an `open` A2UI action → the panel
    re-streams with `?page_id=`, populating `open` (the rich reader card is
    WP-4b; for now the body renders through a bound markdown primitive).
    """
    component = {
        "id": "root",
        "component": "WikiSurface",
        "pages": {"path": "/pages"},
        "open": {"path": "/open"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"pages": pages, "open": open_page},
    )


def artifacts_surface_v09(
    *,
    sources: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    surface_id: str = "library",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Data-bound Library tab. Data model: ``{sources: [...], artifacts: [...]}``.

    Ingested `sources` (raw PDFs/webpages/docs) alongside built `artifacts`.
    Each source carries a bound ``normalized_preview`` (WP-4e) so `SourceCard`
    renders its text preview from props with no self-fetch.
    """
    component = {
        "id": "root",
        "component": "ArtifactsSurface",
        "sources": {"path": "/sources"},
        "artifacts": {"path": "/artifacts"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"sources": sources, "artifacts": artifacts},
    )


def notes_surface_v09(
    *,
    notes: list[dict[str, Any]],
    surface_id: str = "notes",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Data-bound Notes tab. Data model: ``{notes: [{id, title, body_md,
    section_id, updated_at}]}``. Editing a note body is an `edit_note` action
    through the router; the debounced edit patches the model in place."""
    component = {
        "id": "root",
        "component": "NotesSurface",
        "notes": {"path": "/notes"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"notes": notes},
    )


def hypotheses_surface_v09(
    *,
    items: list[dict[str, Any]],
    ach: dict[str, Any] | None = None,
    surface_id: str = "hypotheses",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Data-bound Hypotheses tab. Data model: ``{items: [...], ach: {...} |
    null}``. `items` is the tracked-hypothesis list; `ach` is the ACH matrix
    (null when there is no evidence yet)."""
    component = {
        "id": "root",
        "component": "HypothesesSurface",
        "items": {"path": "/items"},
        "ach": {"path": "/ach"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"items": items, "ach": ach},
    )


def briefs_surface_v09(
    *,
    badge_count: int = 0,
    children: list[dict[str, Any]] | None = None,
    surface_id: str = "briefs",
) -> list[dict[str, Any]]:
    return _surface_messages(
        surface_id=surface_id,
        component_name="BriefsSurface",
        props={"badge_count": badge_count},
        children=children or [],
    )
