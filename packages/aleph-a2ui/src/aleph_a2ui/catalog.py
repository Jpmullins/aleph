"""Aleph A2UI Catalog — loaded from the one canonical file.

The catalog is the contract between Aleph's Python agent SDK and the
`@a2ui/react` renderer in the web app. The renderer validates every inbound
surface/component payload against this schema; the Python SDK validates
outbound. Mismatches are logged and rejected.

**This module holds no component definitions.** It used to: 614 lines of
literals, mirrored by hand in `apps/web/src/a2ui/catalog.ts` and again in a
~265-line object literal inside `copilot-runtime/src/server.ts`. Nothing tied
the three together, and they drifted in ways that stayed invisible until an
agent hit them — `ClaimCard.confidence` listed no `"cited"` (the value both wiki
writers hardcode, so a real card would have failed validation), the agent-facing
copy offered `"initial"` (recognised by nothing) while omitting `"retracted"`
(making the WP-6 state unemittable), and `"dismiss"` was declared dispatchable
with no handler.

The definitions now live in `catalog.json` beside this file, which is the only
copy a human edits. `scripts/gen_catalog.py` renders the two TypeScript views
from it, and `scripts/check-catalog-generated.sh` fails the build if either
drifts. Not being able to disagree is a stronger guarantee than noticing that
you have.

The schema is JSON Schema draft 2020-12. Component shapes are intentionally
permissive on `data_bindings` (JSON pointers) and `children` (recursive
component refs) — the catalog defines the WHAT, A2UI's renderer handles the HOW.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Final

import jsonschema

CATALOG_PATH: Final[pathlib.Path] = pathlib.Path(__file__).with_name("catalog.json")

_RAW: Final[dict[str, Any]] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

CATALOG_VERSION: Final[str] = _RAW["version"]
CATALOG_ID: Final[str] = _RAW["catalogId"]

#: `name -> JSON Schema`. The `agent` block an entry may also carry belongs to
#: the agent-facing catalog and is deliberately not part of validation: the
#: server accepts the full shape, and the agent is shown a narrower one.
_COMPONENTS: Final[dict[str, Any]] = {
    name: entry["schema"] for name, entry in _RAW["components"].items()
}

_ACTIONS: Final[dict[str, Any]] = _RAW["actions"]

CATALOG: dict[str, Any] = {
    "$schema": _RAW["$schema"],
    "$id": _RAW["$id"],
    "title": _RAW["title"],
    "catalogId": CATALOG_ID,
    "version": CATALOG_VERSION,
    "components": _COMPONENTS,
    "actions": _ACTIONS,
}


def catalog_schema_json() -> str:
    return json.dumps(CATALOG, indent=2, sort_keys=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class CatalogValidationError(Exception):
    pass


def validate_component(payload: dict) -> None:
    """Raise CatalogValidationError if `payload` does not match the catalog component
    schema indicated by `payload["type"]`."""
    if not isinstance(payload, dict):
        msg = "component payload must be a dict"
        raise CatalogValidationError(msg)
    type_name = payload.get("type")
    if type_name not in _COMPONENTS:
        msg = f"unknown A2UI component type: {type_name!r}"
        raise CatalogValidationError(msg)
    try:
        jsonschema.validate(payload, _COMPONENTS[type_name])
    except jsonschema.ValidationError as exc:
        msg = f"A2UI component {type_name} failed schema: {exc.message}"
        raise CatalogValidationError(msg) from exc


def validate_surface(payload: dict) -> None:
    """Validate the surface component plus every component in its children tree."""
    validate_component(payload)
    for child in payload.get("children") or []:
        if isinstance(child, dict):
            validate_surface(child)
