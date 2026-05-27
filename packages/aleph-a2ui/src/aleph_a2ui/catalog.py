"""Aleph A2UI Catalog v1.0.0.

The catalog is the contract between Aleph's Python agent SDK and the
`@a2ui/react` renderer in the web app. The renderer validates every
inbound surface/component payload against this schema; the Python SDK
validates outbound. Mismatches are logged and rejected.

The schema is JSON Schema draft 2020-12. The component shapes are
intentionally permissive on `data_bindings` (JSON pointers) and
`children` (recursive component refs) — the catalog defines the WHAT,
A2UI's renderer handles the HOW.
"""

from __future__ import annotations

import json
from typing import Any, Final

import jsonschema

CATALOG_VERSION: Final[str] = "1.0.0"
CATALOG_ID: Final[str] = "aleph-v1"


# ---------------------------------------------------------------------------
# Component schemas
# ---------------------------------------------------------------------------


def _comp(props: dict, *, required: list[str] | None = None) -> dict:
    """Standard component schema wrapper."""
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "id": {"type": "string"},
            "props": {
                "type": "object",
                "properties": props,
                "required": required or [],
                "additionalProperties": True,
            },
            "data_bindings": {"type": "object"},
            "children": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["type", "id", "props"],
        "additionalProperties": False,
    }


_UUID = {"type": "string", "format": "uuid"}


_COMPONENTS = {
    # ----- Surfaces ---------------------------------------------------------
    "WikiSurface": _comp(
        {
            "current_page_id": {"type": ["string", "null"]},
            "view_mode": {"enum": ["page", "graph", "list", "recent"]},
            "filters": {"type": "object"},
        },
        required=["view_mode"],
    ),
    "ArtifactsSurface": _comp(
        {"current_artifact_id": {"type": ["string", "null"]}},
    ),
    "NotesSurface": _comp(
        {
            "current_note_id": {"type": ["string", "null"]},
            "current_section_id": {"type": ["string", "null"]},
        },
    ),
    "HypothesesSurface": _comp(
        {"current_hypothesis_id": {"type": ["string", "null"]}},
    ),
    "BriefsSurface": _comp(
        {
            "filters": {"type": "object"},
            "badge_count": {"type": "integer", "minimum": 0},
        },
    ),
    # ----- Inline cards -----------------------------------------------------
    "ClaimCard": _comp(
        {
            "claim_id": _UUID,
            "text": {"type": "string", "maxLength": 2048},
            "confidence": {
                "enum": ["well-supported", "weakly-supported", "contested", "uncited"]
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "marker": {"type": "string"},
                        "source_short_id": {"type": ["string", "null"]},
                        "chunk_ids": {"type": "array", "items": _UUID},
                    },
                },
            },
            "open_action": {"const": "open"},
        },
        required=["claim_id", "text", "confidence"],
    ),
    "SourceCard": _comp(
        {
            "source_id": _UUID,
            "short_id": {"type": "string"},
            "title": {"type": "string"},
            "url": {"type": ["string", "null"]},
            "status": {"type": "string"},
            "open_action": {"const": "open"},
            "navigate_wiki_action": {"const": "navigate_wiki"},
        },
        required=["source_id", "short_id", "title"],
    ),
    "ChartCard": _comp(
        {
            "dataset_version_id": {"type": ["string", "null"]},
            "vega_lite_spec": {"type": "object"},
            "title": {"type": "string"},
            "open_action": {"const": "open"},
            "_placeholder": {
                "type": "boolean",
                "description": "True while DatasetVersion (Inc 6) hasn't landed.",
            },
        },
    ),
    "TableCard": _comp(
        {
            "dataset_version_id": {"type": ["string", "null"]},
            "columns": {"type": "array", "items": {"type": "object"}},
            "rows": {"type": "array"},
            "title": {"type": "string"},
            "open_action": {"const": "open"},
        },
    ),
    "MapCard": _comp(
        {
            "dataset_version_id": {"type": ["string", "null"]},
            "maplibre_style_url": {"type": "string"},
            "geo_features": {"type": "array"},
            "title": {"type": "string"},
        },
    ),
    "GraphCard": _comp(
        {
            "dataset_version_id": {"type": ["string", "null"]},
            "nodes": {"type": "array"},
            "edges": {"type": "array"},
            "title": {"type": "string"},
        },
    ),
    "ApprovalCard": _comp(
        {
            "target_id": _UUID,
            "target_kind": {
                "enum": ["synthesis_proposal", "review_finding", "wiki_revision"]
            },
            "title": {"type": "string", "maxLength": 200},
            "summary": {"type": "string", "maxLength": 1000},
            "severity": {"enum": ["info", "low", "medium", "high"]},
            "evidence_refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"enum": ["claim", "source", "chunk", "page"]},
                        "id": _UUID,
                        "label": {"type": "string"},
                    },
                },
            },
            "diff_card_id": {"type": ["string", "null"]},
            "approve_action": {"const": "approve"},
            "reject_action": {"const": "reject"},
            "view_diff_action": {"const": "open"},
        },
        required=[
            "target_id",
            "target_kind",
            "title",
            "summary",
            "approve_action",
            "reject_action",
        ],
    ),
    "FindingCard": _comp(
        {
            "finding_id": _UUID,
            "severity": {"enum": ["info", "low", "medium", "high"]},
            "kind": {"type": "string"},
            "summary": {"type": "string"},
            "evidence_refs": {"type": "array"},
            "open_action": {"const": "open"},
            "approve_action": {"const": "approve"},
            "reject_action": {"const": "reject"},
        },
        required=["finding_id", "severity", "kind", "summary"],
    ),
    "HypothesisCard": _comp(
        {
            "hypothesis_id": _UUID,
            "title": {"type": "string"},
            "confidence": {
                "enum": ["initial", "weakly-supported", "well-supported", "rejected"]
            },
            "evidence_count": {"type": "integer"},
            "open_action": {"const": "open"},
        },
        required=["hypothesis_id", "title"],
    ),
    "NotebookCellCard": _comp(
        {
            "section_id": _UUID,
            "body_md": {"type": "string"},
            "ordinal": {"type": "integer"},
            "edit_action": {"const": "edit_note"},
        },
        required=["section_id", "body_md"],
    ),
    "FormCard": _comp(
        {
            "form_id": {"type": "string"},
            "title": {"type": "string"},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                        "type": {
                            "enum": ["text", "textarea", "select", "boolean"]
                        },
                        "options": {"type": "array"},
                        "required": {"type": "boolean"},
                    },
                    "required": ["name", "label", "type"],
                },
            },
            "submit_action": {"const": "submit_form"},
        },
        required=["form_id", "title", "fields"],
    ),
    "DiffCard": _comp(
        {
            "from_revision_id": _UUID,
            "to_revision_id": _UUID,
            "page_id": _UUID,
            "open_action": {"const": "open"},
        },
        required=["from_revision_id", "to_revision_id", "page_id"],
    ),
}


# ---------------------------------------------------------------------------
# Action contract
# ---------------------------------------------------------------------------

_ACTIONS = {
    "approve": {
        "params": {
            "type": "object",
            "properties": {
                "target_id": _UUID,
                "target_kind": {"type": "string"},
            },
            "required": ["target_id", "target_kind"],
        }
    },
    "reject": {
        "params": {
            "type": "object",
            "properties": {
                "target_id": _UUID,
                "target_kind": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["target_id", "target_kind", "reason"],
        }
    },
    "open": {
        "params": {
            "type": "object",
            "properties": {
                "target_id": _UUID,
                "target_kind": {"type": "string"},
            },
            "required": ["target_id", "target_kind"],
        }
    },
    "navigate_wiki": {
        "params": {
            "type": "object",
            "properties": {"page_id": _UUID},
            "required": ["page_id"],
        }
    },
    "submit_form": {
        "params": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string"},
                "values": {"type": "object"},
            },
            "required": ["form_id", "values"],
        }
    },
    "create_hypothesis": {"params": {"type": "object"}},
    "edit_note": {
        "params": {
            "type": "object",
            "properties": {
                "section_id": _UUID,
                "body_md": {"type": "string"},
            },
            "required": ["section_id", "body_md"],
        }
    },
    "clarify": {
        "params": {
            "type": "object",
            "properties": {
                "agent_run_id": _UUID,
                "answer": {"type": "string"},
            },
            "required": ["agent_run_id", "answer"],
        }
    },
    "mark_handedit": {
        "params": {
            "type": "object",
            "properties": {
                "page_id": _UUID,
                "section_anchor": {"type": "string"},
            },
            "required": ["page_id", "section_anchor"],
        }
    },
    "clear_handedit": {
        "params": {
            "type": "object",
            "properties": {
                "page_id": _UUID,
                "section_anchor": {"type": "string"},
            },
            "required": ["page_id", "section_anchor"],
        }
    },
}


CATALOG: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://aleph.research/a2ui/catalog/v1.json",
    "title": "Aleph A2UI Catalog",
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
