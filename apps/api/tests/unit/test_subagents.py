"""Unit tests for the Wave 3 subagent wiring (delegation surface).

These run under `pytest -m "not integration"` — no compose stack, no LLM. They
build each of the six purpose-built subagents and assert the deepagents
`SubAgent` dict is shaped correctly and that each subagent's model carries the
per-subagent cost-attribution callback (rule #5: every subagent LLM call writes
a `ModelCall` + `CostLedgerEvent` tagged `assistant.subagent.<name>`).

Building a subagent constructs a gateway-pointed `ChatOpenAI` but never calls
it, so there is no network. We introspect the attached
`AgentCostCallbackHandler.purpose` to prove the cost tag.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_openai import ChatOpenAI

from aleph_api.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """A minimal Settings built from explicit kwargs (init wins over env)."""
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://aleph:x@localhost:5432/aleph",
        redis_url="redis://localhost:6379/0",
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        otel_exporter_otlp_endpoint="http://localhost:4317",
        litellm_base_url="http://localhost:18999",
        insights_litellm_api_key="test-key",
        aleph_agent_token_secret="test-secret",
        # WS-P7: credential encryption has its own required key now. It is
        # never the signing secret, and it is not padded — hence 64 bytes here
        # rather than "test-secret".
        aleph_credential_master_key="c" * 64,
    )


def _builders() -> dict[str, Any]:
    from aleph_api.subagents.analyst import build_analyst_subagent
    from aleph_api.subagents.researcher import build_researcher_subagent
    from aleph_api.subagents.retriever import build_retriever_subagent
    from aleph_api.subagents.reviewer import build_reviewer_subagent
    from aleph_api.subagents.viz_builder import build_viz_builder_subagent
    from aleph_api.subagents.wiki_builder import build_wiki_builder_subagent

    return {
        "retriever": build_retriever_subagent,
        "researcher": build_researcher_subagent,
        "wiki_builder": build_wiki_builder_subagent,
        "viz_builder": build_viz_builder_subagent,
        "analyst": build_analyst_subagent,
        "reviewer": build_reviewer_subagent,
    }


def _purpose_of(model: ChatOpenAI) -> str | None:
    callbacks = model.callbacks
    if not isinstance(callbacks, list) or not callbacks:
        return None
    return getattr(callbacks[0], "_purpose", None)


@pytest.mark.parametrize(
    "name",
    ["retriever", "researcher", "wiki_builder", "viz_builder", "analyst", "reviewer"],
)
def test_subagent_builds_with_required_shape(settings: Settings, name: str) -> None:
    sub = _builders()[name](settings=settings)

    assert sub["name"] == name
    assert isinstance(sub["description"], str) and sub["description"].strip()
    assert isinstance(sub["system_prompt"], str) and sub["system_prompt"].strip()
    assert isinstance(sub["tools"], list) and len(sub["tools"]) >= 1
    assert isinstance(sub["model"], ChatOpenAI)


@pytest.mark.parametrize(
    "name",
    ["retriever", "researcher", "wiki_builder", "viz_builder", "analyst", "reviewer"],
)
def test_subagent_model_is_cost_tagged(settings: Settings, name: str) -> None:
    # Rule #5: the subagent's model writes cost rows tagged per-subagent.
    sub = _builders()[name](settings=settings)
    assert _purpose_of(sub["model"]) == f"assistant.subagent.{name}"


def test_subagent_model_points_at_gateway(settings: Settings) -> None:
    # Rule #2: agent LLM traffic must route through the configured gateway.
    #
    # The base must be the gateway *plus* `/v1`: openai-python appends only
    # `/chat/completions`, whereas `LiteLLMClient` appends `/v1/chat/completions`
    # to the same setting. Asserting raw equality here is what let the missing
    # `/v1` ship — every agent turn 404'd against a strict OpenAI-compatible
    # server while this test stayed green.
    sub = _builders()["retriever"](settings=settings)
    model: ChatOpenAI = sub["model"]
    base = str(model.openai_api_base)
    assert base.startswith(settings.litellm_base_url.rstrip("/"))
    assert base.rstrip("/").endswith("/v1")


def test_all_six_subagent_names_are_distinct() -> None:
    builders = _builders()
    assert set(builders) == {
        "retriever",
        "researcher",
        "wiki_builder",
        "viz_builder",
        "analyst",
        "reviewer",
    }


def test_subagent_model_helper_tags_arbitrary_name(settings: Settings) -> None:
    from aleph_api.copilot_agent import subagent_model

    model = subagent_model(settings, "echo")
    assert _purpose_of(model) == "assistant.subagent.echo"
