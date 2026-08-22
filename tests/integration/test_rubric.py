"""The grader's spend is recorded, against a real Postgres (`WS-H3`).

`RubricMiddleware` runs a whole second agent per graded turn. Without this it
would be the one model on the agent path calling the gateway with nobody
counting — the hole CLAUDE.md's cost rule exists to close ("every LLM/embed call
writes a `ModelCall` + `CostLedgerEvent`"). A grading feature that cannot be
costed cannot be argued about, and the plan's own risk section for `WS-H3` is
entirely about that cost.

Everything except the database is real. `grader_model(settings)` builds the
production `ChatOpenAI` through `_gateway_chat_model`, `AgentCostCallbackHandler`
and all, and it talks to `aleph_models.testing.FakeGateway` over an in-process
ASGI transport: no socket, no live gateway, and nothing stubbed between the
model and the ledger row.

Three things are pinned here that nothing else can pin:

  * the grader's `ModelCall` rows carry `purpose='assistant.rubric.grader'`, so
    "what did self-grading cost this project" is a query rather than an opinion;
  * they carry the turn's `agent_run_id`, which is what makes
    `scripts/_acceptance/agent_turn_probe.py` count grading in its per-turn
    upstream-request number instead of under-reporting a graded turn;
  * the graded turn issues four upstream chat completions where an ungraded one
    issues one — measured at the transport, not inferred.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.rubric import RUBRIC_GRADER_PURPOSE, RUBRIC_PATH, build_grading_middleware
from aleph_db.models.cost import ModelCall

pytestmark = pytest.mark.integration

RUBRIC = "1. Cite a source for every claim."

#: Token counts on every scripted reply. Non-zero on purpose: a row with zero
#: tokens and a call that was never recorded look identical on a spend
#: dashboard, and telling those two apart is the point of the ledger.
_USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


def _answer_body(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-answer",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-chat",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": _USAGE,
    }


def _verdict_body(result: str, *, gap: str | None = None) -> dict[str, Any]:
    """A gateway chat completion carrying one `GraderResponse`.

    As JSON in the message *content*, not as a tool call. `create_agent` picks
    the structured-output strategy from what the model advertises, and
    `ChatOpenAI` advertises native `json_schema` — so the provider strategy
    parses `message.content`, where the tool strategy would have read
    `tool_calls`. Getting this wrong does not fail loudly at the seam: it
    surfaces as `StructuredOutputValidationError` several frames away, which is
    why the shape is stated here rather than assumed.
    """
    criteria = (
        [{"name": "cites a source", "passed": False, "gap": gap}]
        if gap is not None
        else [{"name": "cites a source", "passed": True}]
    )
    payload = {
        "result": result,
        "explanation": gap or "every criterion passes",
        "criteria": criteria,
    }
    return {
        "id": f"chatcmpl-grade-{result}",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
            }
        ],
        "usage": _USAGE,
    }


def _settings(base_url: str, api_key: str) -> Any:
    from aleph_api.settings import Settings

    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://aleph:x@localhost:5432/aleph",
        redis_url="redis://localhost:6379/0",
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        otel_exporter_otlp_endpoint="http://localhost:4317",
        litellm_base_url=base_url,
        insights_litellm_api_key=api_key,
        aleph_agent_token_secret="integration-secret-0123456789abcdef0123456789abcdef",
        aleph_credential_master_key="c" * 64,
    )


class _RubricBackend:
    """The one file `ProjectRubricMiddleware` reads, shaped like a real backend."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        class _Result:
            error = None if file_path in self._files else f"File '{file_path}' not found"
            file_data = {"content": self._files[file_path]} if file_path in self._files else None

        return _Result()


async def _grader_rows(maker: Callable[[], AsyncSession], project_id: uuid.UUID) -> list[ModelCall]:
    async with maker() as session:
        rows = await session.execute(
            select(ModelCall)
            .where(ModelCall.project_id == project_id)
            .where(ModelCall.purpose == RUBRIC_GRADER_PURPOSE)
        )
        return list(rows.scalars().all())


async def _drive_graded_turn(
    *,
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
) -> int:
    """Run one turn through the real grading middleware. Returns upstream requests."""
    from deepagents import create_deep_agent
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import MemorySaver

    from aleph_api import copilot_agent
    from aleph_models.pricing import PricingTable
    from aleph_models.testing import FakeGateway, GatewayConfig, ScriptedResponse

    fake = FakeGateway(
        GatewayConfig.well_behaved(
            invoke_script=(
                # Interleaved exactly as the loop runs: answer, grade, revise,
                # grade. The fake consumes the script in order, so a change in
                # the loop's shape shows up as a wrong reply rather than as a
                # silently different number.
                ScriptedResponse(status=200, body=_answer_body("first try")),
                ScriptedResponse(status=200, body=_verdict_body("needs_revision", gap="no source")),
                ScriptedResponse(status=200, body=_answer_body("revised, with [1]")),
                ScriptedResponse(status=200, body=_verdict_body("satisfied")),
            )
        )
    )
    http = fake.client()
    # `_gateway_chat_model` builds its own limited client; this is the only seam
    # that can point it at an in-process gateway.
    monkeypatch.setattr("aleph_models.limiter.shared_gateway_client", lambda *_a, **_k: http)

    settings = _settings(fake.base_url, fake.api_key)
    previous = dict(copilot_agent._runtime)
    copilot_agent.bind_runtime(
        session_maker=maker,  # ty: ignore[invalid-argument-type]
        settings=settings,
        pricing=PricingTable(),
    )
    try:
        # The ORCHESTRATOR's model here is a bare `ChatOpenAI` with no cost
        # callback, deliberately: it makes the ledger count unambiguous. Every
        # `assistant.rubric.grader` row in the database was written by the
        # grader and by nothing else.
        agent = create_deep_agent(
            model=ChatOpenAI(
                model="fake-chat",
                api_key=fake.api_key,  # ty: ignore[invalid-argument-type]
                base_url=fake.base_url + "/v1",
                http_async_client=http,
                max_retries=0,
            ),
            tools=[],
            middleware=build_grading_middleware(
                settings=settings,
                backend_factory=lambda _rt: _RubricBackend(files),
                max_iterations=2,
            ),
            checkpointer=MemorySaver(),
        )
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "answer me"}]},
            {
                "configurable": {
                    # The project rides the thread id and the run id rides
                    # `configurable` — the two channels the cost callback reads.
                    "thread_id": f"proj:{project_id}:rubric",
                    "agent_run_id": str(run_id),
                }
            },
        )
    finally:
        copilot_agent._runtime.clear()
        copilot_agent._runtime.update(previous)
        await http.aclose()

    return fake.count("/v1/chat/completions")


@pytest.mark.asyncio
async def test_grader_calls_write_a_model_call(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `ModelCall` per grader invocation, tagged, counted and attributed.

    FAILS without `grader_model`. `RubricMiddleware` also accepts a model
    *string*, which it resolves through `init_chat_model` — a client with no
    callbacks at all. Every grader call would then reach the gateway with no row
    behind it, and the spend would appear nowhere.
    """
    run_id = uuid.uuid4()
    upstream = await _drive_graded_turn(
        maker=maker,
        project_id=committed_project,
        run_id=run_id,
        monkeypatch=monkeypatch,
        files={RUBRIC_PATH: RUBRIC},
    )
    rows = await _grader_rows(maker, committed_project)

    assert upstream == 4, "answer, grade, revise, grade"
    assert len(rows) == 2, (
        "one ModelCall per grader invocation — an uncounted grader call is the "
        "whole hole this closes"
    )
    assert {r.purpose for r in rows} == {RUBRIC_GRADER_PURPOSE}
    assert all(r.input_tokens == 120 and r.completion_tokens == 30 for r in rows)


@pytest.mark.asyncio
async def test_the_grader_rows_join_to_the_turn(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`agent_run_id`, or the turn probe under-reports every graded turn.

    `scripts/_acceptance/agent_turn_probe.py` counts a turn's upstream requests
    with `select count(*) from model_calls where agent_run_id = :rid`. A grader
    row with a NULL run id is spend the probe cannot see, so the number it
    prints would say grading is free.
    """
    run_id = uuid.uuid4()
    await _drive_graded_turn(
        maker=maker,
        project_id=committed_project,
        run_id=run_id,
        monkeypatch=monkeypatch,
        files={RUBRIC_PATH: RUBRIC},
    )
    rows = await _grader_rows(maker, committed_project)

    assert rows, "no grader rows to attribute"
    assert {r.agent_run_id for r in rows} == {run_id}


@pytest.mark.asyncio
async def test_no_rubric_writes_no_grader_row(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inert means inert: no rubric file, no grader call, no spend, one request.

    The counterpart to the test above, and the one that makes it mean something.
    Two rows and zero rows both look correct in isolation; the pair is what
    shows the rows come from grading rather than from being in the graph.
    """
    upstream = await _drive_graded_turn(
        maker=maker,
        project_id=committed_project,
        run_id=uuid.uuid4(),
        monkeypatch=monkeypatch,
        files={},
    )
    rows = await _grader_rows(maker, committed_project)

    assert upstream == 1, "an ungraded turn is one upstream request"
    assert rows == []
