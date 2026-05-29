"""A2UI v0.9 server-to-client message builders (Wave 4).

Pure, I/O-free dict builders that emit the exact wire shape the upstream
`@a2ui/web_core` `MessageProcessor` consumes. Verified against
`apps/web/node_modules/@a2ui/web_core/src/v0_9/schema/server-to-client.d.ts`
and the working spike `apps/web/src/a2ui/_spike/SpikePanel.tsx`.

Each message is a *nested envelope* keyed by its kind, e.g.

    {"version": "v0.9", "createSurface": {"surfaceId": ..., "catalogId": ...}}
    {"version": "v0.9", "updateComponents": {"surfaceId": ..., "components": [...]}}
    {"version": "v0.9", "updateDataModel": {"surfaceId": ..., "path": ..., "value": ...}}

Component props (including data bindings `{"path": "/json/pointer"}`) are passed
INLINE on each component object, keyed by the schema prop name — not nested under
a `props` key. The data model is delivered as a single bulk `updateDataModel`
with `path="/"` and the full object as `value` (the wire supports a root path),
matching SpikePanel.
"""

from __future__ import annotations

from typing import Any

A2UI_VERSION = "v0.9"


def create_surface(*, surface_id: str, catalog_id: str) -> dict[str, Any]:
    """Build a `createSurface` message binding a surface to a catalog."""
    return {
        "version": A2UI_VERSION,
        "createSurface": {"surfaceId": surface_id, "catalogId": catalog_id},
    }


def update_components(*, surface_id: str, components: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an `updateComponents` message.

    Each entry in `components` must carry at least `component` (the catalog
    component-type name) and typically `id`; all other keys are props passed
    inline (literals or `{"path": ...}` bindings).
    """
    return {
        "version": A2UI_VERSION,
        "updateComponents": {"surfaceId": surface_id, "components": components},
    }


def update_data_model(*, surface_id: str, path: str, value: Any) -> dict[str, Any]:
    """Build an `updateDataModel` message patching `path` to `value`."""
    return {
        "version": A2UI_VERSION,
        "updateDataModel": {
            "surfaceId": surface_id,
            "path": path,
            "value": value,
        },
    }


def full_surface(
    *,
    surface_id: str,
    catalog_id: str,
    components: list[dict[str, Any]],
    data_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the ordered message list to render a surface from scratch.

    Order: `createSurface` -> `updateComponents` -> a single bulk
    `updateDataModel` rooted at `/` carrying the whole data model (the wire
    accepts a root path + full value, so one message seeds every binding;
    later per-path `update_data_model` calls patch individual leaves — that is
    the T6 delta path).
    """
    messages: list[dict[str, Any]] = [
        create_surface(surface_id=surface_id, catalog_id=catalog_id),
        update_components(surface_id=surface_id, components=components),
    ]
    if data_model is not None:
        messages.append(update_data_model(surface_id=surface_id, path="/", value=data_model))
    return messages
