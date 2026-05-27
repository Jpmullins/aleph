"""ModelProfile resolver tests."""

from __future__ import annotations

import pytest

from aleph_core.errors import ValidationFailed
from aleph_core.schemas.model_profile import Capability
from aleph_models.profile import resolve_binding


def test_resolve_known_capability() -> None:
    bindings = {
        "synthesis": {
            "model": "claude-opus-4-7",
            "provider": "litellm",
            "max_input_tokens": 200000,
        }
    }
    b = resolve_binding(bindings, Capability.SYNTHESIS)
    assert b.model == "claude-opus-4-7"
    assert b.provider == "litellm"
    assert b.max_input_tokens == 200_000


def test_resolve_missing_capability_raises() -> None:
    with pytest.raises(ValidationFailed):
        resolve_binding({}, Capability.PAGE_SELECTION)


def test_resolve_with_string_capability_key() -> None:
    bindings = {"embedding": {"model": "cohere-embed-v4", "provider": "litellm"}}
    b = resolve_binding(bindings, "embedding")
    assert b.model == "cohere-embed-v4"
