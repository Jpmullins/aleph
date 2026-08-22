"""A secret submitted through a settings screen must not land in an append-only table.

Two layers, because either alone fails in a way the other catches.

`settings_card.SecretFieldRefused` stops a schema that DECLARES a secret
(`format: password`, `writeOnly: true`) from being rendered as a settings field
at all. That is the strong half — the schema said what it was — and it cannot be
fooled by a field name.

`action_router.redact_secrets` matches on the key NAME at the persistence
boundary. Weaker, and it catches what the first cannot: a plain
`{"type": "string"}` field called `api_key`, or a secret arriving through an
action nobody generated from a schema.

The reason both are needed: `ActionRouter.dispatch` persists the params AND the
handler's result to `card_actions.params_jsonb` and to the hash-chained ledger
payload. Both are append-only. A secret written there cannot be deleted — only
regretted, and then rotated.
"""

from __future__ import annotations

from typing import Any

import pytest

from aleph_a2ui.action_router import REDACTED, redact_secrets
from aleph_a2ui.settings_card import SecretFieldRefused, settings_components


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "api_key",
        "apiKey",
        "API_KEY",
        "client_secret",
        "access_key",
        "bearer_token",
        "credential",
        "private_key",
        "authorization",
    ],
)
def test_a_secret_shaped_key_is_redacted(key: str) -> None:
    assert redact_secrets({key: "hunter2"}) == {key: REDACTED}


def test_redaction_reaches_nested_values() -> None:
    """A settings payload nests, and so does a plugin's config."""
    payload = {
        "plugin": "gateway",
        "config": {"endpoint": "http://x", "api_key": "sk-live-123"},
        "history": [{"token": "t1"}, {"note": "fine"}],
    }
    out = redact_secrets(payload)
    assert isinstance(out, dict)
    assert out["config"]["api_key"] == REDACTED
    assert out["config"]["endpoint"] == "http://x", "a non-secret was redacted"
    assert out["history"][0]["token"] == REDACTED
    assert out["history"][1]["note"] == "fine"


def test_the_marker_is_distinguishable_from_a_blank_field() -> None:
    """An auditor has to tell "withheld" from "the user left it empty"."""
    assert REDACTED != ""
    assert redact_secrets({"password": ""}) == {"password": REDACTED}


def test_a_cyclic_payload_does_not_hang_the_audit_write() -> None:
    """A hand-built dict can be cyclic, and an append-only write must complete."""
    node: dict[str, Any] = {"name": "a"}
    node["self"] = node
    redact_secrets(node)


def test_a_schema_declaring_a_secret_is_refused_before_it_can_be_submitted() -> None:
    """The strong half. `variant: "obscured"` hides it on screen and nowhere else."""
    schema = {"type": "object", "properties": {"token": {"type": "string", "writeOnly": True}}}
    with pytest.raises(SecretFieldRefused):
        settings_components(plugin_title="P", config_schema=schema, plugin_id="p")


def test_a_secret_by_name_only_still_reaches_the_screen_and_is_redacted_on_write() -> None:
    """The case each layer handles alone, stated so the division is deliberate.

    A field called `api_key` with no `format` and no `writeOnly` is not
    self-declared, so the generator has no grounds to refuse it — and it renders
    as an ordinary text box. The router is what stops the value being persisted.
    """
    schema = {"type": "object", "properties": {"api_key": {"type": "string"}}}
    components = settings_components(plugin_title="P", config_schema=schema, plugin_id="p")
    assert any(c["id"].endswith("-api_key") for c in components)
    assert redact_secrets({"api_key": "sk-live"})["api_key"] == REDACTED  # ty: ignore[index]


def test_an_ordinary_payload_is_returned_unchanged() -> None:
    """Over-redaction costs an audit trail its content."""
    payload = {"enabled": True, "endpoint": "http://x", "runs": [1, 2, 3], "note": None}
    assert redact_secrets(payload) == payload
