"""Surface builders — typed wrappers that emit catalog-conformant payloads."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from aleph_a2ui.messages import create_surface, full_surface, update_components

# Catalog id of the shared v0.9 frontend catalog
# (`apps/web/src/a2ui/aleph-catalog-v09.tsx`). `createSurface.catalogId` must
# reference this exact value for the renderer to resolve component impls.
ALEPH_V09_CATALOG_ID = "aleph://v1"


def _surface(
    type_name: str,
    *,
    surface_id: str | None,
    props: dict[str, Any],
    children: list[dict[str, Any]] | None = None,
    data_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": type_name,
        "id": surface_id or f"{type_name}-{uuid4().hex[:8]}",
        "props": props,
        "data_bindings": data_bindings or {},
        "children": children or [],
    }


def wiki_surface(
    *,
    current_page_id: UUID | None = None,
    view_mode: str = "page",
    filters: dict[str, Any] | None = None,
    children: list[dict[str, Any]] | None = None,
    surface_id: str | None = None,
) -> dict[str, Any]:
    return _surface(
        "WikiSurface",
        surface_id=surface_id,
        props={
            "current_page_id": str(current_page_id) if current_page_id else None,
            "view_mode": view_mode,
            "filters": filters or {},
        },
        children=children,
    )


def artifacts_surface(
    *,
    current_artifact_id: UUID | None = None,
    surface_id: str | None = None,
) -> dict[str, Any]:
    return _surface(
        "ArtifactsSurface",
        surface_id=surface_id,
        props={
            "current_artifact_id": str(current_artifact_id) if current_artifact_id else None,
        },
    )


def notes_surface(
    *,
    current_note_id: UUID | None = None,
    current_section_id: UUID | None = None,
    children: list[dict[str, Any]] | None = None,
    surface_id: str | None = None,
) -> dict[str, Any]:
    return _surface(
        "NotesSurface",
        surface_id=surface_id,
        props={
            "current_note_id": str(current_note_id) if current_note_id else None,
            "current_section_id": str(current_section_id) if current_section_id else None,
        },
        children=children,
    )


def hypotheses_surface(
    *,
    current_hypothesis_id: UUID | None = None,
    children: list[dict[str, Any]] | None = None,
    surface_id: str | None = None,
) -> dict[str, Any]:
    return _surface(
        "HypothesesSurface",
        surface_id=surface_id,
        props={
            "current_hypothesis_id": str(current_hypothesis_id) if current_hypothesis_id else None,
        },
        children=children,
    )


def hypotheses_surface_v09(
    *,
    hypotheses: list[dict[str, Any]],
    surface_id: str = "hypotheses",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Build the A2UI v0.9 message list for the Hypotheses right-panel tab.

    Renders one `HypothesisCard` per hypothesis inside a `Column`. Card props
    are data BINDINGS into `/items/<i>/...` so a later per-path `updateDataModel`
    (Wave 4 T6) can patch confidence/evidence in place without re-sending the
    component tree. `hypotheses` is a list of dicts with keys `hypothesis_id`,
    `title`, `confidence`, `evidence_count`.
    """
    components: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    card_ids: list[str] = []
    for i, h in enumerate(hypotheses):
        cid = f"hyp-card-{i}"
        card_ids.append(cid)
        components.append(
            {
                "id": cid,
                "component": "HypothesisCard",
                "hypothesis_id": {"path": f"/items/{i}/hypothesis_id"},
                "title": {"path": f"/items/{i}/title"},
                "confidence": {"path": f"/items/{i}/confidence"},
                "evidence_count": {"path": f"/items/{i}/evidence_count"},
            }
        )
        items.append(
            {
                "hypothesis_id": str(h.get("hypothesis_id", "")),
                "title": str(h.get("title", "")),
                "confidence": str(h.get("confidence", "")),
                "evidence_count": int(h.get("evidence_count", 0) or 0),
            }
        )
    # Root Column wraps the cards (basic-catalog primitive, merged into the
    # shared catalog alongside HypothesisCard).
    components.insert(0, {"id": "root", "component": "Column", "children": card_ids})
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=components,
        data_model={"items": items},
    )


def briefs_surface(
    *,
    badge_count: int = 0,
    filters: dict[str, Any] | None = None,
    children: list[dict[str, Any]] | None = None,
    surface_id: str | None = None,
) -> dict[str, Any]:
    return _surface(
        "BriefsSurface",
        surface_id=surface_id,
        props={
            "badge_count": badge_count,
            "filters": filters or {},
        },
        children=children,
    )


# ---------------------------------------------------------------------------
# v0.9 message-list builders (Wave 4 T3)
# ---------------------------------------------------------------------------
#
# Each right-panel tab is rendered through the upstream `@a2ui` v0_9
# `MessageProcessor` + `<A2uiSurface>` against the shared catalog (`aleph://v1`).
# The rich Aleph surface views (`WikiSurface`/`ArtifactsSurface`/`NotesSurface`/
# `HypothesesSurface`/`BriefsSurface`) are registered as single v0_9 components
# and fetch their own data client-side, so each builder emits exactly ONE
# top-level surface component plus an (optional) child card list. `children`
# rides INLINE on the component object as a structural prop (the frontend
# `adapt` helper forwards it to `component.children`).


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
    when no such component exists. (`hypotheses_surface_v09` satisfies this via
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


def wiki_surface_v09(
    *,
    current_page_id: UUID | None = None,
    view_mode: str = "page",
    children: list[dict[str, Any]] | None = None,
    surface_id: str = "wiki",
) -> list[dict[str, Any]]:
    return _surface_messages(
        surface_id=surface_id,
        component_name="WikiSurface",
        props={
            "current_page_id": str(current_page_id) if current_page_id else "",
            "view_mode": view_mode,
        },
        children=children or [],
    )


def artifacts_surface_v09(
    *,
    current_artifact_id: UUID | None = None,
    surface_id: str = "artifacts",
) -> list[dict[str, Any]]:
    return _surface_messages(
        surface_id=surface_id,
        component_name="ArtifactsSurface",
        props={
            "current_artifact_id": str(current_artifact_id) if current_artifact_id else "",
        },
    )


def notes_surface_v09(
    *,
    children: list[dict[str, Any]] | None = None,
    surface_id: str = "notes",
) -> list[dict[str, Any]]:
    return _surface_messages(
        surface_id=surface_id,
        component_name="NotesSurface",
        children=children or [],
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
