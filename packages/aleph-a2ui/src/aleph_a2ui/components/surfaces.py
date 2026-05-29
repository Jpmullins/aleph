"""Surface builders — typed wrappers that emit catalog-conformant payloads."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from aleph_a2ui.messages import full_surface

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
