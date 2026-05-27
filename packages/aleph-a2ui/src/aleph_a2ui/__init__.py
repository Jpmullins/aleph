"""Aleph A2UI catalog + integration.

Catalog v1.0.0 ships 5 Surface components (one per right-panel tab) plus
12 inline cards used in chat and embedded inside surfaces. The catalog
JSON Schema is the source of truth; Python typed builders + a parallel
TypeScript catalog (generated) keep both ends honest.
"""

from aleph_a2ui.action_router import ActionRouter, CardActionRequest, CardActionResult
from aleph_a2ui.catalog import (
    CATALOG,
    CATALOG_VERSION,
    catalog_schema_json,
    validate_component,
    validate_surface,
)
from aleph_a2ui.models import CardAction, InteractiveCard, InteractiveCardVersion

__all__ = [
    "ActionRouter",
    "CATALOG",
    "CATALOG_VERSION",
    "CardAction",
    "CardActionRequest",
    "CardActionResult",
    "InteractiveCard",
    "InteractiveCardVersion",
    "catalog_schema_json",
    "validate_component",
    "validate_surface",
]
