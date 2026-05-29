"""Unit tests for the agent LLM cost-attribution callback (Wave 6 C1).

These run with `pytest -m "not integration"` — no compose stack. We feed
fake LangChain run lifecycle events to `AgentCostCallbackHandler` and assert
it records exactly one ModelCall + CostLedgerEvent via CostWriter.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from aleph_api.copilot_cost_callback import AgentCostCallbackHandler
from aleph_models.pricing import PricingTable

PROJECT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "test-project")


class _FakeSession:
    """Captures `session.add(...)` rows; no-op flush/commit/context-manager."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:  # noqa: D401
        return None

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSessionMaker:
    """Async-sessionmaker stand-in: returns the SAME session each call so the
    test can inspect what got added."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


async def test_records_one_model_call_with_correct_tokens() -> None:
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=PricingTable(),
        model="claude-sonnet-4-6",
    )
    run_id = uuid.uuid4()

    await handler.on_chat_model_start(
        serialized={},
        messages=[[]],
        run_id=run_id,
        metadata={"projectId": str(PROJECT_ID)},
    )

    gen = ChatGeneration(
        message=AIMessage(
            content="hi",
            usage_metadata={
                "input_tokens": 1200,
                "output_tokens": 350,
                "total_tokens": 1550,
                "input_token_details": {"cache_read": 200},
            },
        )
    )
    result = LLMResult(generations=[[gen]])

    await handler.on_llm_end(result, run_id=run_id)

    # ModelCall + CostLedgerEvent.
    from aleph_db.models.cost import CostLedgerEvent, ModelCall

    model_calls = [r for r in session.added if isinstance(r, ModelCall)]
    events = [r for r in session.added if isinstance(r, CostLedgerEvent)]
    assert len(model_calls) == 1
    assert len(events) == 1

    call = model_calls[0]
    assert call.project_id == PROJECT_ID
    assert call.purpose == "assistant.turn"
    assert call.capability == "chat"
    assert call.model == "claude-sonnet-4-6"
    assert call.input_tokens == 1200
    assert call.cached_tokens == 200
    assert call.completion_tokens == 350
    assert call.cost_usd > 0


async def test_extracts_from_llm_output_token_usage_shape() -> None:
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=PricingTable(),
        model="claude-sonnet-4-6",
    )
    run_id = uuid.uuid4()
    await handler.on_llm_start(
        serialized={},
        prompts=["x"],
        run_id=run_id,
        metadata={"projectId": str(PROJECT_ID)},
    )

    # Legacy OpenAI-style llm_output.token_usage shape, no usage_metadata.
    gen = ChatGeneration(message=AIMessage(content="hi"))
    result = LLMResult(
        generations=[[gen]],
        llm_output={
            "token_usage": {
                "prompt_tokens": 800,
                "completion_tokens": 120,
                "total_tokens": 920,
            }
        },
    )
    await handler.on_llm_end(result, run_id=run_id)

    from aleph_db.models.cost import ModelCall

    calls = [r for r in session.added if isinstance(r, ModelCall)]
    assert len(calls) == 1
    assert calls[0].input_tokens == 800
    assert calls[0].completion_tokens == 120
    assert calls[0].cached_tokens == 0


async def test_skips_when_no_project_id() -> None:
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=PricingTable(),
        model="claude-sonnet-4-6",
    )
    run_id = uuid.uuid4()
    # No metadata/configurable carrying a project → must not write.
    await handler.on_chat_model_start(serialized={}, messages=[[]], run_id=run_id)
    gen = ChatGeneration(
        message=AIMessage(
            content="hi",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    )
    await handler.on_llm_end(LLMResult(generations=[[gen]]), run_id=run_id)
    assert session.added == []


async def test_skips_when_no_usage() -> None:
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=PricingTable(),
        model="claude-sonnet-4-6",
    )
    run_id = uuid.uuid4()
    await handler.on_chat_model_start(
        serialized={},
        messages=[[]],
        run_id=run_id,
        metadata={"projectId": str(PROJECT_ID)},
    )
    gen = ChatGeneration(message=AIMessage(content="hi"))
    await handler.on_llm_end(LLMResult(generations=[[gen]]), run_id=run_id)
    assert session.added == []
