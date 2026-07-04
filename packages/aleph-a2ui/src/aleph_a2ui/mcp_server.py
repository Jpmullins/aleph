"""A2UI-over-MCP server for Aleph's catalog.

Exposes Aleph's A2UI catalog and surface payloads over the Model Context
Protocol so any MCP client (another agent, an inspector, a cross-tool host) can
introspect the catalog and request schema-valid surfaces — the pattern from the
upstream `a2ui-over-mcp` recipe, but serving **Aleph's** catalog
(`aleph_a2ui.catalog`) instead of the basic catalog.

- Resource `a2ui://aleph/catalog` → the Aleph catalog JSON Schema (component +
  action definitions). Lets a client discover what components exist.
- Tool `list_surfaces` → the surface names Aleph can build.
- Tool `build_surface {surface}` → a validated v0.9 A2UI message list for that
  surface, returned as an `application/a2ui+json` embedded resource.

Run with: `python -m aleph_a2ui.mcp_server` (stdio transport). Requires the
optional `mcp` extra: `uv sync --all-packages --all-extras` (or
`pip install 'aleph-a2ui[mcp]'`).
"""

from __future__ import annotations

# The MCP server SDK ships untyped low-level decorators (`@app.list_resources()`
# etc.); suppress the strict untyped-decorator error for this optional
# entrypoint, consistent with how the repo treats untyped framework glue.
# pyright: reportUntypedFunctionDecorator=false
import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from aleph_a2ui.catalog import CATALOG, catalog_schema_json
from aleph_a2ui.components.surfaces import (
    artifacts_surface_v09,
    briefs_surface_v09,
    hypotheses_surface_v09,
    notes_surface_v09,
    wiki_surface_v09,
)

A2UI_MIME_TYPE = "application/a2ui+json"
CATALOG_RESOURCE_URI = "a2ui://aleph/catalog"

# surface name -> zero-arg builder returning a v0.9 message list.
# The canonical tabs are data-bound (WP-4): their builders take loaded rows.
# The MCP `build_surface` tool has no DB session, so it emits the empty-data
# SKELETON surface (structure + bound `{path}` props + an empty data model) —
# the shape an agent inspects, populated at stream time by the route layer.
_SURFACE_BUILDERS: dict[str, Any] = {
    "wiki": lambda: wiki_surface_v09(pages=[], open_page=None),
    "artifacts": lambda: artifacts_surface_v09(sources=[], artifacts=[]),
    "notes": lambda: notes_surface_v09(notes=[]),
    "hypotheses": lambda: hypotheses_surface_v09(items=[], ach=None),
    "briefs": briefs_surface_v09,
}


_VALID_MESSAGE_KEYS = {"createSurface", "updateComponents", "updateDataModel", "deleteSurface"}


def _validate_messages(messages: list[dict[str, Any]]) -> None:
    """Validate a surface message list is a well-formed v0.9 wire message sequence.

    The Aleph catalog JSON Schema describes component *prop* shapes (type/id/
    props), while surface builders emit the v0.9 *wire* shape (id/component with
    inline props), so we validate the message envelope structure here: the list
    must start with a `createSurface` and every message must carry exactly one
    known operation key. Components reference catalog component names by their
    `component` field, which a client can cross-check against the catalog
    resource.
    """
    if not messages or "createSurface" not in messages[0]:
        msg = "surface message list must begin with createSurface"
        raise ValueError(msg)
    for m in messages:
        ops = _VALID_MESSAGE_KEYS & set(m)
        if len(ops) != 1:
            msg = f"message has no single known operation key: {sorted(m)}"
            raise ValueError(msg)


def build_app() -> Server:
    app: Server = Server("aleph-a2ui-mcp")

    @app.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri=CATALOG_RESOURCE_URI,  # type: ignore[arg-type]
                name="Aleph A2UI Catalog",
                mimeType="application/schema+json",
                description=(
                    f"Aleph A2UI catalog {CATALOG['version']} "
                    f"({len(CATALOG['components'])} components, "
                    f"{len(CATALOG['actions'])} actions)."
                ),
            )
        ]

    @app.read_resource()
    async def read_resource(uri: Any) -> str:
        if str(uri) == CATALOG_RESOURCE_URI:
            return catalog_schema_json()
        msg = f"unknown resource: {uri}"
        raise ValueError(msg)

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="list_surfaces",
                description="List the A2UI surfaces Aleph can build.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="build_surface",
                description=(
                    "Build a schema-valid A2UI v0.9 surface message list for one of "
                    "Aleph's surfaces. Returns an application/a2ui+json resource."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "surface": {
                            "type": "string",
                            "enum": sorted(_SURFACE_BUILDERS),
                        }
                    },
                    "required": ["surface"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.ContentBlock]:
        if name == "list_surfaces":
            return [types.TextContent(type="text", text=json.dumps(sorted(_SURFACE_BUILDERS)))]
        if name == "build_surface":
            surface = arguments.get("surface")
            builder = _SURFACE_BUILDERS.get(surface or "")
            if builder is None:
                msg = f"unknown surface: {surface!r}"
                raise ValueError(msg)
            messages = builder()
            _validate_messages(messages)
            return [
                types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=f"a2ui://aleph/surface/{surface}",  # type: ignore[arg-type]
                        mimeType=A2UI_MIME_TYPE,
                        text=json.dumps(messages),
                    ),
                )
            ]
        msg = f"unknown tool: {name}"
        raise ValueError(msg)

    return app


async def _run_stdio() -> None:
    from mcp.server.stdio import stdio_server

    app = build_app()
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    import anyio

    anyio.run(_run_stdio)


if __name__ == "__main__":
    main()
