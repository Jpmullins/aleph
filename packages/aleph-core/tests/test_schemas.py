"""Pydantic schema round-trip tests."""

from __future__ import annotations

from aleph_core.schemas.model_profile import (
    Capability,
    ModelBindingIn,
    ModelProfileUpdate,
)
from aleph_core.schemas.project import ProjectCreate


def test_project_create_defaults() -> None:
    p = ProjectCreate(title="x")
    assert p.model_profile_name == "aleph-dev"


def test_project_create_rejects_bad_profile() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProjectCreate(title="x", model_profile_name="aleph-fast")


def test_model_binding_provider_must_be_litellm() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelBindingIn(model="x", provider="openai")  # type: ignore[arg-type]

    b = ModelBindingIn(model="claude-opus-4-7")
    assert b.provider == "litellm"


def test_model_profile_update_capability_enum() -> None:
    payload = {"bindings": {"synthesis": {"model": "claude-opus-4-7"}}}
    upd = ModelProfileUpdate.model_validate(payload)
    assert Capability.SYNTHESIS in upd.bindings
    assert upd.bindings[Capability.SYNTHESIS].model == "claude-opus-4-7"
