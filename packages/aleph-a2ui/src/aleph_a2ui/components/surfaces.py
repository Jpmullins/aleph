"""Surface builders — typed wrappers that emit catalog-conformant payloads."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4


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
