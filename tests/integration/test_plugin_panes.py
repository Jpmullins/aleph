"""A plugin can add a pane, and a broken one cannot take the workspace down.

WS-B1a. `PANE_REGISTRY.extend()` has always described itself as "the seam a
plugin uses" — and the thing that BUILT a pane was a hardcoded if/elif chain in
a 1,028-line route file that raised `NotFound` on any name it did not know. So a
plugin could register a pane and the app would break on it. The door was half
built, and the missing half was the half that does anything.

Worse, that chain ran inside an unguarded loop feeding the SINGLE multiplexed
connection every open pane reads from. An exception escaping it ended the
generator, so one broken pane blanked all of them, with the reason only in the
API's stderr. A plugin's bug became an outage.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_a2ui.components.surfaces import ALEPH_V09_CATALOG_ID
from aleph_a2ui.messages import full_surface
from aleph_a2ui.pane_registry import PANE_REGISTRY, PaneKind
from aleph_api.routes.surfaces import _build_tab_messages, _pane_error_surface

pytestmark = pytest.mark.integration


async def _probe_builder(
    _session: Any, _project_id: uuid.UUID, params: dict[str, str], surface_id: str
) -> list[dict[str, Any]]:
    return full_surface(
        surface_id=surface_id,
        catalog_id=ALEPH_V09_CATALOG_ID,
        components=[{"id": "root", "component": "Text", "text": {"path": "/who"}}],
        data_model={"who": f"probe pane, params={sorted(params.items())}"},
    )


async def _exploding_builder(
    _session: Any, _project_id: uuid.UUID, _params: dict[str, str], _surface_id: str
) -> list[dict[str, Any]]:
    msg = "this plugin is broken"
    raise RuntimeError(msg)


@pytest.fixture
def probe_pane() -> Any:
    """Registered and withdrawn, so one test cannot leak a pane into another."""
    kind = PaneKind(
        id="probe_pane",
        title="Probe pane",
        icon="notes",
        params=("thing_id",),
        builder=_probe_builder,
        source="test",
    )
    PANE_REGISTRY.extend(kind)
    yield kind
    PANE_REGISTRY.remove("probe_pane")


async def test_a_pane_registered_only_through_extend_builds_a_real_surface(
    probe_pane: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Criterion 1. This raised `NotFound` before, for any name not in the chain."""
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "probe_pane", {"thing_id": "abc"}, "probe_pane"
        )
    assert messages
    created = [m for m in messages if "createSurface" in m]
    assert created, "the plugin pane emitted no createSurface"
    assert created[0]["createSurface"]["surfaceId"] == "probe_pane"


async def test_the_plugins_declared_param_reaches_its_builder(
    probe_pane: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A pane that cannot receive its own parameter is a pane that renders one
    thing forever."""
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "probe_pane", {"thing_id": "xyz"}, "probe_pane"
        )
    model = next(
        m["updateDataModel"]["value"]
        for m in reversed(messages)
        if m.get("updateDataModel", {}).get("path") == "/"
    )
    assert "thing_id" in model["who"]
    assert "xyz" in model["who"]


async def test_a_pane_whose_builder_raises_becomes_an_error_surface() -> None:
    """Criterion 2, at the function that produces it.

    The isolation lives in the stream loop; this pins the surface it emits — a
    real, renderable pane that names what broke, rather than an empty frame the
    client would draw as a blank block.
    """
    surface = _pane_error_surface("bad_pane", "bad_pane", RuntimeError("boom"))
    assert surface
    model = next(
        m["updateDataModel"]["value"]
        for m in reversed(surface)
        if m.get("updateDataModel", {}).get("path") == "/"
    )
    assert "bad_pane" in model["message"]
    assert "RuntimeError" in model["message"]
    # The CLASS, not the message. An exception string carries whatever the
    # failing code put in it and this is rendered in a browser.
    assert "boom" not in model["message"]


async def test_a_broken_pane_does_not_prevent_a_good_one_from_building(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The property that makes plugin panes safe to offer at all.

    Driven through the same helper the stream loop calls, once per pane, with
    the loop's own try/except reproduced — the loop itself needs a live SSE
    client, and what matters is that one failure is contained rather than how
    the bytes are framed.
    """
    PANE_REGISTRY.extend(
        PaneKind(id="bad_pane", title="Bad", icon="notes", builder=_exploding_builder)
    )
    try:
        results: dict[str, list[dict[str, Any]]] = {}
        async with maker() as session:
            for pane in ("wiki", "bad_pane"):
                try:
                    results[pane] = await _build_tab_messages(
                        session, committed_project, pane, {}, pane
                    )
                except Exception as exc:
                    results[pane] = _pane_error_surface(pane, pane, exc)
    finally:
        PANE_REGISTRY.remove("bad_pane")

    assert results["wiki"], "the healthy pane was lost with the broken one"
    bad = next(
        m["updateDataModel"]["value"]
        for m in reversed(results["bad_pane"])
        if m.get("updateDataModel", {}).get("path") == "/"
    )
    assert "RuntimeError" in bad["message"]


def test_the_stream_loop_actually_guards_each_pane() -> None:
    """The test above reproduces the loop's handling; this asserts the loop HAS it.

    Without this, the isolation could be deleted from `_build_all` and every
    test here would stay green — the guarded behaviour would live only in the
    test that models it.
    """
    import ast
    import pathlib

    src = pathlib.Path("apps/api/src/aleph_api/routes/surfaces.py")
    tree = ast.parse(src.read_text())
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "_build_all":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Try) and any(
                "_pane_error_surface" in ast.unparse(h) for h in inner.handlers
            ):
                guarded = True
    assert guarded, (
        "_build_all does not catch a pane's failure — one broken pane will end "
        "the multiplexed stream and blank the whole workspace"
    )


def test_a_registered_pane_id_is_not_derived_from_its_title() -> None:
    """Criterion 3, already true since the Inspector landed.

    `_parse_pane_specs` used to read one hardcoded key, so a pane whose id was
    not its lowercased title could not be addressed at all.
    """
    from aleph_api.routes.surfaces import _parse_pane_specs

    PANE_REGISTRY.extend(
        PaneKind(id="claim_map", title="Claim map", icon="notes", params=("claim_id",))
    )
    try:
        specs = _parse_pane_specs("claim_map:claim_id=c1")
        assert specs == [("claim_map:claim_id=c1", "claim_map", {"claim_id": "c1"})]
    finally:
        PANE_REGISTRY.remove("claim_map")
