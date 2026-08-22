"""Summarisation fires against the model's real window, not a fixed 170k.

WS-MEP-7 c1. `deepagents.compute_summarization_defaults` branches on
`model.profile`: with one it uses fractions of the model's own window, and with
none it uses `("tokens", 170000)` for every model on every gateway. Aleph built
every agent model with no profile, so a 32k local model would blow its context
long before summarisation triggered, and a 1M model summarised at 17% of its
window for nothing. Seven models are built through `_gateway_chat_model` — the
orchestrator and six subagents — so this is one line in one constructor and
seven agents' behaviour.

These assert what deepagents COMPUTES, not what Aleph passes. `profile=` being
set is a fact about Aleph; the trigger changing is the fact that matters, and
only the second one goes red if deepagents changes the key it reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from deepagents.middleware.summarization import compute_summarization_defaults

from aleph_api import copilot_agent


def _settings() -> Any:
    return SimpleNamespace(
        litellm_base_url="http://gateway.invalid",
        insights_litellm_api_key="sk-not-a-real-key",
        aleph_agent_request_timeout_s=120.0,
    )


@pytest.fixture(autouse=True)
def _clear_bindings() -> Any:
    """`_runtime` is process-global; a test that binds must unbind."""
    previous = copilot_agent._runtime.pop("agent_bindings", None)
    yield
    copilot_agent._runtime.pop("agent_bindings", None)
    if previous is not None:
        copilot_agent._runtime["agent_bindings"] = previous


def test_a_bound_window_makes_summarisation_fractional() -> None:
    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "vllm-local-qwen3-32b", "max_input_tokens": 32_768}
    }
    model = copilot_agent._gateway_chat_model(_settings(), purpose="test")
    assert model.profile == {"max_input_tokens": 32_768}
    assert compute_summarization_defaults(model)["trigger"] == ("fraction", 0.85)


def test_every_subagent_gets_the_window_too() -> None:
    """Six of the seven models are subagents; the orchestrator alone is not the fix."""
    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "vllm-local-qwen3-32b", "max_input_tokens": 32_768}
    }
    model = copilot_agent.subagent_model(_settings(), "researcher")
    assert compute_summarization_defaults(model)["trigger"] == ("fraction", 0.85)


def test_a_binding_that_declares_no_window_gets_none_invented() -> None:
    """A guessed 200k is the same defect as a fixed 170k, wearing a profile.

    This is the case the plan's correction #13 names: a gateway that reports no
    context window for a model. `ModelBindingIn.max_input_tokens` defaults to
    200_000 and `resolve_binding` substitutes the same number for an absent key,
    so reading the window through either would report 200k for a model nobody
    measured. `_resolve_agent_context_window` reads the RAW binding instead and
    leaves it unset.
    """
    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "a-model-of-unknown-size", "provider": "litellm"}
    }
    model = copilot_agent._gateway_chat_model(_settings(), purpose="test")
    assert model.model_name == "a-model-of-unknown-size"
    assert model.profile is None
    assert compute_summarization_defaults(model)["trigger"] == ("tokens", 170_000)


def test_a_window_of_zero_is_not_a_window() -> None:
    """`profile={"max_input_tokens": 0}` would make every trigger 0 tokens."""
    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "a-model", "max_input_tokens": 0}
    }
    assert copilot_agent._gateway_chat_model(_settings(), purpose="test").profile is None


def test_nothing_bound_at_all_is_an_error_not_a_window() -> None:
    """Building against no profile fails; it does not build with no window.

    Pops the directory fixture's bindings deliberately — the unbound case has to
    stay reachable, or the conftest would be hiding the behaviour it supports.
    """
    copilot_agent._runtime.pop("agent_bindings", None)
    with pytest.raises(copilot_agent.NoModelBound):
        copilot_agent._gateway_chat_model(_settings(), purpose="test")
