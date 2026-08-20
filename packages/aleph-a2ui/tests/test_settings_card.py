"""The generated settings surface must be renderable, not merely well-shaped.

Three of these tests check the failure modes that make a generated screen look
finished while being inert: a child id nothing defines, a binding pointing at a
path the data model never seeds, and a save button that forgets a field. None of
those raise at render time — the screen just quietly does less than it appears
to, which is precisely the defect class this repo keeps paying for.

`test_every_component_matches_the_upstream_schema` is the strongest one: it
reads `basic_catalog.json` out of the installed `@a2ui/web_core` and checks each
emitted component against the real upstream definition. That is what makes this
a check against the renderer's actual contract rather than against my reading of
it — if upstream renames a prop or adds a required one, this goes red.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from aleph_a2ui.settings_card import (
    SETTINGS_SAVE_ACTION,
    settings_components,
    settings_data_model,
    settings_surface,
)

CATALOG_ID = "aleph://v1"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "endpoint": {
            "type": "string",
            "title": "Gateway endpoint",
            "description": "Where Aleph reaches your models.",
        },
        "api_key": {"type": "string", "format": "password"},
        "enabled": {"type": "boolean", "default": True},
        "max_concurrent_runs": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
        "timeout_seconds": {"type": "integer", "default": 30},
        "mode": {"type": "string", "enum": ["fast", "balanced", "thorough"], "default": "balanced"},
        "capabilities": {"type": "array", "items": {"enum": ["chat", "embed", "rerank"]}},
        "notes": {"type": "string", "maxLength": 4000},
        "starts_at": {"type": "string", "format": "date-time"},
    },
}


def _components() -> list[dict[str, Any]]:
    return settings_components(
        plugin_title="Model gateway", config_schema=SCHEMA, plugin_id="gateway"
    )


def _by_id(components: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in components}


def test_every_child_id_resolves_to_a_component() -> None:
    """A dangling child id renders nothing and reports no error."""
    components = _components()
    ids = set(_by_id(components))
    root = _by_id(components)["root"]
    missing = [c for c in root["children"] if c not in ids]
    assert missing == [], f"root references ids that do not exist: {missing}"

    for comp in components:
        child = comp.get("child")
        if isinstance(child, str):
            assert child in ids, f"{comp['id']}.child -> unknown id {child!r}"


def test_every_bound_path_is_seeded_in_the_data_model() -> None:
    """An unseeded binding shows an empty control the user cannot tell is broken."""
    components = _components()
    model = settings_data_model(config_schema=SCHEMA)

    bound: set[str] = set()
    for comp in components:
        value = comp.get("value")
        if isinstance(value, dict) and "path" in value:
            bound.add(str(value["path"]).lstrip("/"))

    assert bound, "no field bound to the data model at all"
    unseeded = sorted(p for p in bound if p not in model)
    assert unseeded == [], f"bound but never seeded: {unseeded}"


def test_save_carries_every_field_plus_the_plugin_id() -> None:
    """A field missing from the action context is silently un-saveable."""
    components = _components()
    action = _by_id(components)["save"]["action"]
    context = action["event"]["context"]

    assert action["event"]["name"] == SETTINGS_SAVE_ACTION
    assert context["pluginId"] == "gateway"
    for key in SCHEMA["properties"]:
        assert key in context, f"{key} would never reach the server"
        assert context[key] == {"path": f"/{key}"}


@pytest.mark.parametrize(
    ("field_key", "expected"),
    [
        ("endpoint", "TextField"),
        ("api_key", "TextField"),
        ("enabled", "CheckBox"),
        ("max_concurrent_runs", "Slider"),
        ("timeout_seconds", "TextField"),
        ("mode", "ChoicePicker"),
        ("capabilities", "ChoicePicker"),
        ("notes", "TextField"),
        ("starts_at", "DateTimeInput"),
    ],
)
def test_schema_type_picks_the_right_control(field_key: str, expected: str) -> None:
    comps = _by_id(_components())
    match = [c for cid, c in comps.items() if cid.endswith(f"-{field_key}")]
    assert match, f"no control emitted for {field_key}"
    assert match[0]["component"] == expected


def test_password_is_obscured_and_long_text_is_long() -> None:
    comps = _by_id(_components())
    assert comps["f1-api_key"]["variant"] == "obscured"
    assert comps["f7-notes"]["variant"] == "longText"
    assert comps["f0-endpoint"]["variant"] == "shortText"


def test_an_unrenderable_field_is_stated_not_dropped() -> None:
    """Silently skipping a field yields a screen that cannot configure the thing."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"weird": {"type": "object"}, "ok": {"type": "boolean"}},
    }
    components = settings_components(plugin_title="P", config_schema=schema, plugin_id="p")
    root = _by_id(components)["root"]
    weird = [c for c in components if c["id"].endswith("-weird")]
    assert weird, "the unsupported field vanished from the surface entirely"
    assert weird[0]["id"] in root["children"], "emitted but never mounted"
    assert "not editable here" in weird[0]["text"]


def test_defaults_and_stored_values_both_land() -> None:
    model = settings_data_model(config_schema=SCHEMA, current={"endpoint": "http://x"})
    assert model["endpoint"] == "http://x"  # stored wins
    assert model["enabled"] is True  # schema default
    assert model["capabilities"] == []  # empty list, not missing
    assert model["api_key"] == ""  # seeded even with no default


def test_surface_emits_create_then_components_then_data() -> None:
    messages = settings_surface(
        plugin_id="gateway",
        plugin_title="Model gateway",
        config_schema=SCHEMA,
        catalog_id=CATALOG_ID,
    )
    kinds = [next(k for k in m if k != "version") for m in messages]
    assert kinds == ["createSurface", "updateComponents", "updateDataModel"]
    assert messages[0]["createSurface"]["catalogId"] == CATALOG_ID
    assert messages[0]["createSurface"]["surfaceId"] == "settings:gateway"


def _upstream_basic_catalog() -> dict[str, Any] | None:
    root = pathlib.Path(__file__).resolve().parents[3]
    p = root / "apps/web/node_modules/@a2ui/web_core/src/v0_9/schemas/basic_catalog.json"
    return json.loads(p.read_text()) if p.is_file() else None


def test_every_component_matches_the_upstream_schema() -> None:
    """Check the emitted props against `@a2ui/web_core`'s own catalog definition."""
    catalog = _upstream_basic_catalog()
    if catalog is None:
        pytest.skip("@a2ui/web_core not installed — run pnpm -C apps/web install")

    defs: dict[str, Any] = catalog["components"]

    def required_props(name: str) -> set[str]:
        node = defs[name]
        req: set[str] = set()
        for part in [node, *node.get("allOf", [])]:
            req |= set(part.get("required", []))
        return req - {"component"}

    for comp in _components():
        name = comp["component"]
        assert name in defs, f"{name} is not a basic-catalog component"
        missing = required_props(name) - set(comp)
        assert missing == set(), f"{comp['id']} ({name}) missing required props: {missing}"
