"""Unit tests for the WP-4d agent eyes+hands action schemas.

The ActionRouter validates every card action's params against
`CATALOG["actions"][kind]["params"]` before dispatching. These tests pin the
new navigation/composition action contracts (focus_tab / highlight_claim /
compose_dossier / spotlight) and the slug-or-page_id navigate_wiki shape.
No DB, no I/O — run under `pytest -m "not integration"`.
"""

from __future__ import annotations

from uuid import uuid4

import jsonschema
import pytest

from aleph_a2ui.catalog import CATALOG


def _params_schema(action_kind: str) -> dict:
    spec = CATALOG["actions"][action_kind]
    return spec["params"]


def _valid(action_kind: str, params: dict) -> None:
    jsonschema.validate(params, _params_schema(action_kind))


def _invalid(action_kind: str, params: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(params, _params_schema(action_kind))


def test_new_actions_are_registered_in_catalog() -> None:
    for kind in ("focus_tab", "highlight_claim", "compose_dossier", "spotlight"):
        assert kind in CATALOG["actions"], f"{kind} missing from catalog actions"


def test_focus_tab_accepts_valid_tab_rejects_unknown() -> None:
    _valid("focus_tab", {"tab": "Hypotheses"})
    _invalid("focus_tab", {"tab": "NotATab"})
    _invalid("focus_tab", {})


def test_highlight_claim_requires_claim_id() -> None:
    # `format: uuid` is annotation-only (the router registers no format checker,
    # so the handler's `UUID()` rejects bad ids); the schema enforces presence.
    _valid("highlight_claim", {"claim_id": str(uuid4())})
    _invalid("highlight_claim", {})


def test_compose_dossier_requires_title_and_typed_id_arrays() -> None:
    _valid("compose_dossier", {"title": "My dossier"})
    _valid(
        "compose_dossier",
        {"title": "Grouped", "page_ids": [str(uuid4())], "card_ids": [str(uuid4())]},
    )
    _invalid("compose_dossier", {})  # title required
    _invalid("compose_dossier", {"title": "x", "page_ids": "not-a-list"})  # must be array


def test_spotlight_requires_card_id() -> None:
    _valid("spotlight", {"card_id": str(uuid4())})
    _invalid("spotlight", {})


def test_navigate_wiki_accepts_page_id_or_slug_not_neither() -> None:
    _valid("navigate_wiki", {"page_id": str(uuid4())})
    _valid("navigate_wiki", {"slug": "some-page"})
    _invalid("navigate_wiki", {})  # anyOf: at least one of page_id / slug
