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


def test_an_unbound_capability_gets_no_invented_window() -> None:
    """A guessed 200k is the same defect as a fixed 170k, wearing a profile."""
    model = copilot_agent._gateway_chat_model(_settings(), purpose="test")
    assert model.profile is None
    assert compute_summarization_defaults(model)["trigger"] == ("tokens", 170_000)
