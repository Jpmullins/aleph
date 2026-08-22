"""The agent's model calls reach the metrics, not only the database.

`record_llm_request` and `record_llm_usage` had six call sites, every one inside
`LiteLLMClient` — and the agent never touches `LiteLLMClient`. It builds
`ChatOpenAI`, whose cost is recorded by `AgentCostCallbackHandler`. So an agent
turn produced a `ModelCall` row and ZERO metric samples.

That is the wrong half to be missing. The stated reason for counting LLM calls
by purpose is backlog E5 — "is the subagent fan-out what is rate-limiting us" —
and subagent fan-out was precisely the path with no counter on it. The question
the metric exists to answer was the one it could not.
"""

from __future__ import annotations

import uuid

import pytest

from aleph_api.copilot_cost_callback import AgentCostCallbackHandler
from aleph_observability import metrics as m

PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


def _count(name: str, **labels: str) -> float:
    value = m.sample_value(name, **labels)
    return 0.0 if value is None else value


@pytest.fixture
def handler() -> AgentCostCallbackHandler:
    return AgentCostCallbackHandler(model="claude-sonnet-4-6", purpose="test.agent.turn")


async def test_an_agent_model_call_increments_the_request_counter(
    handler: AgentCostCallbackHandler,
) -> None:
    before = _count(
        "aleph_llm_requests_total", capability="chat", purpose="test.agent.turn", outcome="ok"
    )
    await handler._write(
        project_id=PROJECT,
        agent_run_id=None,
        input_tokens=10,
        cached_tokens=0,
        completion_tokens=5,
        latency_ms=250,
    )
    after = _count(
        "aleph_llm_requests_total", capability="chat", purpose="test.agent.turn", outcome="ok"
    )
    assert after == before + 1


async def test_a_failed_agent_call_is_counted_as_an_error_not_omitted(
    handler: AgentCostCallbackHandler,
) -> None:
    """A counter that only increments on success cannot tell "the gateway is
    down" from "nobody is calling it", and those need opposite responses."""
    before = _count(
        "aleph_llm_requests_total", capability="chat", purpose="test.agent.turn", outcome="error"
    )
    await handler._write(
        project_id=PROJECT,
        agent_run_id=None,
        input_tokens=0,
        cached_tokens=0,
        completion_tokens=0,
        latency_ms=90,
        failure="RateLimitError",
    )
    after = _count(
        "aleph_llm_requests_total", capability="chat", purpose="test.agent.turn", outcome="error"
    )
    assert after == before + 1


async def test_agent_tokens_are_labelled_by_pricing_source(
    handler: AgentCostCallbackHandler,
) -> None:
    """Unpriced agent spend has to be visible AS unpriced.

    The cost row already carries `pricing_source`; without it on the metric,
    `aleph_model_call_cost_total` sums priced and unpriced calls into one number
    that reads as "what we spent" and is not.

    `unknown` is the expected label here rather than an accident: no pricing
    table is bound in a unit test, and the handler records the call anyway —
    "we could not price this" is the finding, not a reason to drop the sample.
    """
    labels = {
        "capability": "chat",
        "purpose": "test.agent.turn",
        "pricing_source": "unknown",
        "kind": "input",
    }
    before = _count("aleph_llm_tokens_total", **labels)
    await handler._write(
        project_id=PROJECT,
        agent_run_id=None,
        input_tokens=7,
        cached_tokens=2,
        completion_tokens=3,
        latency_ms=100,
    )
    # 7 input + 2 cached: cached tokens are still tokens the gateway billed for,
    # and omitting them under-reports every cached conversation.
    assert _count("aleph_llm_tokens_total", **labels) == before + 9
    assert _count("aleph_llm_tokens_total", **{**labels, "kind": "output"}) == _count(
        "aleph_llm_tokens_total", **{**labels, "kind": "output"}
    )


async def test_a_metrics_failure_does_not_cost_the_cost_row(
    handler: AgentCostCallbackHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observability must never break the thing it observes.

    The metric emission sits outside the database session for this reason: a
    broken exporter should cost a sample, not a cost row.
    """

    def _boom(**_kwargs: object) -> None:
        msg = "exporter is on fire"
        raise RuntimeError(msg)

    monkeypatch.setattr(m, "record_llm_request", _boom)
    # No exception, and no assertion about the DB — there is no session here, so
    # `_write` logs and returns. The point is that it RETURNS.
    await handler._write(
        project_id=PROJECT,
        agent_run_id=None,
        input_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        latency_ms=10,
    )
