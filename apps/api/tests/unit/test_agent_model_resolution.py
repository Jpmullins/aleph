"""Agent model resolves from a ModelProfile's bindings (rule #7, audit F06).

The conversational surface must use the profile the project selected rather than
a model id Aleph shipped. Since WS-MEP-6 there is no shipped id to fall back to:
an unbound capability raises `NoModelBound`, naming the capability and the two
ways to bind it, at the moment the graph is built. These are pure unit tests —
they poke the module-bound bindings and assert resolution, no network, no DB.
"""

from __future__ import annotations

import pathlib

import pytest

from aleph_api import copilot_agent
from aleph_api.copilot_cost_callback import _agent_run_id_from_metadata

AGENT_SOURCE = pathlib.Path(copilot_agent.__file__)


@pytest.fixture(autouse=True)
def _restore_runtime():
    saved = copilot_agent._runtime.get("agent_bindings")
    yield
    copilot_agent._runtime["agent_bindings"] = saved


def test_resolves_capability_model_from_bindings() -> None:
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "a-synthesis-model", "provider": "litellm"},
        "code": {"model": "a-code-model", "provider": "litellm"},
    }
    assert copilot_agent._resolve_agent_model(Capability.SYNTHESIS) == "a-synthesis-model"
    assert copilot_agent._resolve_agent_model(Capability.CODE) == "a-code-model"


def test_nothing_bound_is_an_error() -> None:
    """No profile at all is a stated failure, not a model id nobody chose.

    This used to return `_AGENT_MODEL`, which defaulted to a hardcoded Anthropic
    id. On a deployment whose gateway does not serve it, that produced a 404 on
    the agent's traffic only, long after boot, with nothing naming the cause.
    """
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = None
    with pytest.raises(copilot_agent.NoModelBound) as raised:
        copilot_agent._resolve_agent_model(Capability.SYNTHESIS)
    assert raised.value.capability == "synthesis"
    assert "synthesis" in str(raised.value)


def test_an_unbound_capability_is_an_error() -> None:
    """A profile that binds SOME capabilities does not silently cover the rest."""
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "test-synthesis-model", "provider": "litellm"}
    }
    with pytest.raises(copilot_agent.NoModelBound) as raised:
        copilot_agent._resolve_agent_model(Capability.JUDGE)
    assert raised.value.capability == "judge"


def test_the_error_names_both_ways_to_bind_a_model() -> None:
    """An error a reader cannot act on is a crash with better manners."""
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = None
    with pytest.raises(copilot_agent.NoModelBound) as raised:
        copilot_agent._resolve_agent_model(Capability.SYNTHESIS)
    message = str(raised.value)
    assert "model-profile/autoconfigure" in message
    assert copilot_agent.FALLBACK_MODEL_ENV in message


def test_the_operator_escape_hatch_is_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ALEPH_FALLBACK_AGENT_MODEL` still works, and ships no default.

    The env var is how an operator keeps a gateway-specific deployment running
    while a profile is missing. What was deleted is its DEFAULT: a model id
    Aleph shipped, for a gateway it knows nothing about.
    """
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = None
    monkeypatch.setenv(copilot_agent.FALLBACK_MODEL_ENV, "operator-chose-this")
    assert copilot_agent._resolve_agent_model(Capability.SYNTHESIS) == "operator-chose-this"


def test_no_model_id_is_hardcoded_on_the_agent_path() -> None:
    """WS-MEP-6 c4, as a test rather than only as a grep in a plan document.

    Reads the source because the defect is a literal, and a literal that is
    never resolved in these tests is still shipped. Vendor-neutral on purpose:
    the rule is that Aleph names no model, not that it names no Anthropic model.
    """
    import re

    source = AGENT_SOURCE.read_text(encoding="utf-8")
    offenders = [
        (n, line)
        for n, line in enumerate(source.splitlines(), start=1)
        if re.search(
            r"claude-(sonnet|opus|haiku)|gpt-4|titan-embed|aleph-dev|aleph-production", line
        )
    ]
    assert offenders == [], f"copilot_agent.py names models Aleph does not ship: {offenders}"


def test_agent_run_id_extracted_from_metadata() -> None:
    from uuid import uuid4

    rid = uuid4()
    assert _agent_run_id_from_metadata({"agent_run_id": str(rid)}) == rid
    assert _agent_run_id_from_metadata({"agentRunId": str(rid)}) == rid
    assert _agent_run_id_from_metadata({}) is None
    assert _agent_run_id_from_metadata({"agent_run_id": "not-a-uuid"}) is None
