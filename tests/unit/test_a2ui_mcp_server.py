"""Verify the A2UI-over-MCP server: a real stdio client round-trip.

Spawns `python -m aleph_a2ui.mcp_server` as an MCP server over stdio and drives
it with the MCP client SDK — list/read the catalog resource and call the
`build_surface` tool, asserting the returned payload is a valid Aleph surface.

Skipped automatically if the optional `mcp` extra is not installed.
"""

from __future__ import annotations

import json
import sys

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


async def _exercise() -> dict[str, object]:
    def _first_text(blocks: object) -> str:
        for b in blocks:  # type: ignore[attr-defined]
            if getattr(b, "type", None) == "text":
                return b.text
        msg = "no text content block"
        raise AssertionError(msg)

    def _first_resource(blocks: object) -> object:
        for b in blocks:  # type: ignore[attr-defined]
            if getattr(b, "type", None) == "resource":
                return b
        msg = "no embedded resource content block"
        raise AssertionError(msg)

    params = StdioServerParameters(command=sys.executable, args=["-m", "aleph_a2ui.mcp_server"])
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        resources = await session.list_resources()
        catalog = await session.read_resource("a2ui://aleph/catalog")
        tools = await session.list_tools()
        surfaces = await session.call_tool("list_surfaces", {})
        built = await session.call_tool("build_surface", {"surface": "wiki"})

        return {
            "resource_uris": [str(r.uri) for r in resources.resources],
            "catalog_text": catalog.contents[0].text,  # type: ignore[union-attr]
            "tool_names": [t.name for t in tools.tools],
            "surfaces_text": _first_text(surfaces.content),
            "built_embedded": _first_resource(built.content),
        }


def test_mcp_server_exposes_catalog_and_builds_surface() -> None:
    import anyio

    out = anyio.run(_exercise)

    # The catalog resource is advertised and readable.
    assert "a2ui://aleph/catalog" in out["resource_uris"]
    catalog = json.loads(out["catalog_text"])  # type: ignore[arg-type]
    assert catalog["catalogId"] == "aleph-v1"
    assert "WikiSurface" in catalog["components"]

    # Both tools are advertised.
    assert {"list_surfaces", "build_surface"} <= set(out["tool_names"])  # type: ignore[arg-type]

    # list_surfaces returns the five Aleph surfaces.
    assert set(json.loads(out["surfaces_text"])) == {  # type: ignore[arg-type]
        "wiki",
        "artifacts",
        "notes",
        "hypotheses",
        "briefs",
    }

    # build_surface returns an application/a2ui+json embedded resource whose
    # payload is a valid v0.9 message list (createSurface + a WikiSurface root).
    embedded = out["built_embedded"]
    assert embedded.type == "resource"  # type: ignore[union-attr]
    res = embedded.resource  # type: ignore[union-attr]
    assert res.mimeType == "application/a2ui+json"
    messages = json.loads(res.text)
    assert any("createSurface" in m for m in messages)
    components = [c for m in messages for c in m.get("updateComponents", {}).get("components", [])]
    assert any(c.get("component") == "WikiSurface" for c in components)
