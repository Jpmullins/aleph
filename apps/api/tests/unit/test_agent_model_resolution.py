"""Agent model resolves from the default ModelProfile bindings (rule #7, audit F06).

The conversational surface must use the project's selected profile (aleph-dev /
aleph-production) rather than a hardcoded model id. These are pure unit tests:
they poke the module-bound bindings and assert resolution, no network/DB.
"""

from __future__ import annotations

import pytest

from aleph_api import copilot_agent
from aleph_api.copilot_cost_callback import _agent_run_id_from_metadata


@pytest.fixture(autouse=True)
def _restore_runtime():
    saved = copilot_agent._runtime.get("agent_bindings")
    yield
    copilot_agent._runtime["agent_bindings"] = saved


def test_resolves_capability_model_from_bindings() -> None:
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "claude-opus-4-8", "provider": "litellm"},
        "code": {"model": "claude-sonnet-4-6", "provider": "litellm"},
    }
    assert copilot_agent._resolve_agent_model(Capability.SYNTHESIS) == "claude-opus-4-8"
    assert copilot_agent._resolve_agent_model(Capability.CODE) == "claude-sonnet-4-6"


def test_falls_back_when_unbound() -> None:
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = None
    assert copilot_agent._resolve_agent_model(Capability.SYNTHESIS) == copilot_agent._AGENT_MODEL


def test_falls_back_on_unmapped_capability() -> None:
    from aleph_core.schemas.model_profile import Capability

    copilot_agent._runtime["agent_bindings"] = {
        "synthesis": {"model": "claude-opus-4-8", "provider": "litellm"}
    }
    # No 'judge' binding -> fall back, never crash.
    assert copilot_agent._resolve_agent_model(Capability.JUDGE) == copilot_agent._AGENT_MODEL


def test_agent_run_id_extracted_from_metadata() -> None:
    from uuid import uuid4

    rid = uuid4()
    assert _agent_run_id_from_metadata({"agent_run_id": str(rid)}) == rid
    assert _agent_run_id_from_metadata({"agentRunId": str(rid)}) == rid
    assert _agent_run_id_from_metadata({}) is None
    assert _agent_run_id_from_metadata({"agent_run_id": "not-a-uuid"}) is None
