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

**And it is measured under BOTH structured-output strategies, because which one
the grader gets is decided by the model's NAME.** `create_agent` picks the
provider strategy (`response_format: json_schema`, verdict in `message.content`)
when `langchain.agents.factory._supports_provider_strategy` says so, and for a
model that advertises no `structured_output` profile flag — which is every model
Aleph binds, since `_gateway_chat_model` sets `profile` to a context window and
nothing else — that reduces to a hardcoded regex allowlist of vendor ids,
`FALLBACK_MODELS_WITH_STRUCTURED_OUTPUT`. Anything outside it gets the tool
strategy instead (a bound `GraderResponse` tool, verdict in `tool_calls`).

Aleph binds whatever the gateway reports and ships no model list, so BOTH are
live in production: `claude-sonnet-4-6` matches the allowlist, and the same
model served under a gateway alias (`bedrock/anthropic.claude-sonnet-4-6-v1:0`,
`judge-cheap`) does not. The library handles both; the request on the wire is a
different shape in each. So `_drive_graded_turn` is parametrised over the pair,
each case scripts the verdict shape its strategy actually reads, and the test
asserts the strategy it got — otherwise a change in that allowlist would move
this file between two code paths silently, which is exactly what happened to it
once already (see `_bindings`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.rubric import RUBRIC_GRADER_PURPOSE, RUBRIC_PATH, build_grading_middleware
from aleph_db.models.cost import ModelCall

pytestmark = pytest.mark.integration

RUBRIC = "1. Cite a source for every claim."

#: The judge model this module binds when a test does not name one, and the
#: structured-output strategy `langchain` gives it. Synthetic, mirroring
#: `apps/api/tests/unit/conftest.py`: a name that appears in a failure message
#: and is obviously not a model anybody serves.
TOOL_STRATEGY_JUDGE = "test-judge-model"

#: A judge id on `FALLBACK_MODELS_WITH_STRUCTURED_OUTPUT`, so the grader gets
#: `response_format: json_schema` instead. Not an assertion that any gateway
#: serves it — the fake serves whatever the script answers with. It is here
#: because being on that list is the ONLY thing that selects the other code
#: path, and a name off the list cannot exercise it.
PROVIDER_STRATEGY_JUDGE = "claude-sonnet-4-6"


def _bindings(judge_model: str = TOOL_STRATEGY_JUDGE) -> dict[str, Any]:
    """A whole synthetic profile, with `judge` as the parameter under test.

    This module has to bind its own because `copilot_agent._runtime` is
    process-global and `WS-MEP-6` made an unbound capability raise
    `NoModelBound` rather than substituting a hardcoded id. Before this
    function existed these three tests bound none, so they resolved `judge`
    from whatever the *previous test file* had left in `_runtime` — in practice
    the `aleph-dev` template's `claude-sonnet-4-6`, put there by
    `test_route_smoke.py` booting the real lifespan (`lifespan.py:132`) and
    never restoring it.

    Run alone: `NoModelBound`, three failures. Run after `test_route_smoke.py`:
    green. Three tests asserting that the grader's spend is recorded were
    proving nothing about a fresh process — and, worse than order-dependent,
    they were silently pinned to the PROVIDER strategy, because the leaked id
    happens to be on langchain's allowlist and `test-judge-model` is not.
    """
    return {
        "synthesis": {"model": "test-synthesis-model", "provider": "litellm"},
        "judge": {"model": judge_model, "provider": "litellm"},
        "code": {"model": "test-code-model", "provider": "litellm"},
    }


@pytest.fixture(autouse=True)
def isolated_agent_runtime() -> Iterator[None]:
    """No inheriting, no leaking: `_runtime` is restored byte for byte.

    It installs the default bindings by OVERWRITING rather than filling a gap,
    and that is the point: filling a gap would leave the module order-dependent
    in the direction that hides, because a leaked binding would still be the one
    in force. Restoring the whole dict afterwards keeps this file from becoming
    the next one's inherited state.
    """
    from aleph_api import copilot_agent

    previous = dict(copilot_agent._runtime)
    copilot_agent._runtime["agent_bindings"] = _bindings()
    try:
        yield
    finally:
        copilot_agent._runtime.clear()
        copilot_agent._runtime.update(previous)


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


def _verdict_body(result: str, *, gap: str | None = None, strategy: str) -> dict[str, Any]:
    """A gateway chat completion carrying one `GraderResponse`.

    `strategy` decides WHERE the verdict goes, and the two are not
    interchangeable:

    * ``"provider"`` — JSON in `message.content`. What the grader reads when
      `create_agent` sent `response_format: json_schema`.
    * ``"tool"`` — the same JSON as the arguments of a `GraderResponse` tool
      call. What it reads when `create_agent` bound the schema as a tool
      instead.

    Answering with the wrong one does not fail loudly at the seam. Under the
    tool strategy a content-only reply is just an assistant message with no
    tool call, so the grader agent LOOPS — it asks again, eats the next
    scripted response, and eventually surfaces as `grader_error` several frames
    away with the transport count silently wrong. That is what this module did
    for as long as its judge model came from another test file's leak.
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
    if strategy == "provider":
        message: dict[str, Any] = {"role": "assistant", "content": json.dumps(payload)}
        finish = "stop"
    elif strategy == "tool":
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{result}",
                    "type": "function",
                    "function": {"name": "GraderResponse", "arguments": json.dumps(payload)},
                }
            ],
        }
        finish = "tool_calls"
    else:  # pragma: no cover - a typo in a parametrisation, caught immediately
        msg = f"unknown structured-output strategy {strategy!r}"
        raise ValueError(msg)
    return {
        "id": f"chatcmpl-grade-{result}",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-chat",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
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


@dataclass(frozen=True)
class _Turn:
    """What one graded turn did, measured at the transport."""

    #: `/v1/chat/completions` requests the gateway answered.
    upstream: int
    #: The model `grader_model` resolved WHILE the turn's bindings were in
    #: force. Captured inside the bound scope rather than rebuilt afterwards:
    #: rebuilding it outside is what let the old version of this file compare
    #: the ledger against whatever happened to be bound at assertion time.
    grader_model: str
    #: Bodies of the requests made against `grader_model`, so a test can assert
    #: which structured-output strategy the grader actually got.
    grader_requests: tuple[dict[str, Any], ...]


async def _drive_graded_turn(
    *,
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    judge_model: str = TOOL_STRATEGY_JUDGE,
    strategy: str = "tool",
) -> _Turn:
    """Run one turn through the real grading middleware."""
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
                #
                # There is no fifth entry and `judge_model` is not one of the
                # fake's models, so a fifth request is answered 400 "Invalid
                # model name" — a fast, named failure rather than a hang.
                ScriptedResponse(status=200, body=_answer_body("first try")),
                ScriptedResponse(
                    status=200,
                    body=_verdict_body("needs_revision", gap="no source", strategy=strategy),
                ),
                ScriptedResponse(status=200, body=_answer_body("revised, with [1]")),
                ScriptedResponse(status=200, body=_verdict_body("satisfied", strategy=strategy)),
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
        # Production's `bind_runtime` passes this (`lifespan.py:132`) and this
        # call did not, which is the whole of the defect: `grader_model` builds
        # a JUDGE model, and with nothing bound that is `NoModelBound`.
        agent_bindings=_bindings(judge_model),
    )
    try:
        # Inside the bound scope, and before the turn: this is the id the
        # grader is about to run as, not a re-resolution done later against
        # whatever is bound at assertion time.
        from aleph_api.rubric import grader_model

        resolved_grader_model = str(grader_model(settings).model_name)
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

    return _Turn(
        upstream=fake.count("/v1/chat/completions"),
        grader_model=resolved_grader_model,
        grader_requests=tuple(
            r.body
            for r in fake.requests
            if r.body is not None and r.body.get("model") == resolved_grader_model
        ),
    )


def test_this_module_binds_its_own_judge_model() -> None:
    """The defect the other tests here could not report: inherited bindings.

    `grader_model` resolves JUDGE from the AMBIENT `copilot_agent._runtime` —
    the process-global dict `test_route_smoke.py` writes the `aleph-dev`
    template into by booting the real lifespan, and never restores. This is the
    one test in the file that reads it without `_drive_graded_turn` binding
    anything first, so it is what makes `isolated_agent_runtime` load-bearing
    rather than decorative.

    It goes red two different ways, which is the point: with no fixture and no
    leak it raises `NoModelBound`; with no fixture and a leak it resolves
    `claude-sonnet-4-6` and reports whose binding it got.
    """
    from aleph_api.rubric import grader_model

    resolved = grader_model(_settings("http://unused", "unused")).model_name
    assert resolved == TOOL_STRATEGY_JUDGE, (
        f"the JUDGE capability resolves to {resolved!r} with nothing in this "
        f"test bound; this module binds {TOOL_STRATEGY_JUDGE!r}. Another test "
        "file's `_runtime` is in force and this file is order-dependent."
    )


@pytest.mark.parametrize(
    ("judge_model", "strategy"),
    [(TOOL_STRATEGY_JUDGE, "tool"), (PROVIDER_STRATEGY_JUDGE, "provider")],
)
@pytest.mark.asyncio
async def test_grader_calls_write_a_model_call(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    judge_model: str,
    strategy: str,
) -> None:
    """One `ModelCall` per grader invocation, tagged, counted and attributed.

    FAILS without `grader_model`. `RubricMiddleware` also accepts a model
    *string*, which it resolves through `init_chat_model` — a client with no
    callbacks at all. Every grader call would then reach the gateway with no row
    behind it, and the spend would appear nowhere.

    Run twice, once per structured-output strategy. Cost attribution must not
    depend on whether langchain recognised the bound judge id, and until this
    parametrisation existed it was never checked against a model it does not
    recognise — which is most of what a gateway serves.
    """
    run_id = uuid.uuid4()
    turn = await _drive_graded_turn(
        maker=maker,
        project_id=committed_project,
        run_id=run_id,
        monkeypatch=monkeypatch,
        files={RUBRIC_PATH: RUBRIC},
        judge_model=judge_model,
        strategy=strategy,
    )
    rows = await _grader_rows(maker, committed_project)

    # The strategy the grader ACTUALLY got, read off the wire. Asserted rather
    # than assumed because it is chosen from a hardcoded vendor-name allowlist
    # in langchain, not from anything Aleph or the gateway says: if that list
    # changes under us, this parametrisation quietly stops covering two paths
    # and starts covering one twice.
    assert turn.grader_requests, "the grader made no request of its own"
    for body in turn.grader_requests:
        got = "provider" if body.get("response_format") else "tool"
        assert got == strategy, (
            f"bound judge {judge_model!r} and expected the {strategy} strategy, "
            f"but the request on the wire is {got}: "
            f"response_format={body.get('response_format')} "
            f"tools={[t.get('function', {}).get('name') for t in body.get('tools') or []]}"
        )
    # After the strategy, not before: a strategy mismatch makes the grader loop,
    # so checking the count first reports "4 != 2" and buries the cause.
    assert len(turn.grader_requests) == 2, turn.grader_requests

    assert turn.upstream == 4, "answer, grade, revise, grade"
    assert len(rows) == 2, (
        "one ModelCall per grader invocation — an uncounted grader call is the "
        "whole hole this closes"
    )
    assert {r.purpose for r in rows} == {RUBRIC_GRADER_PURPOSE}
    assert all(r.input_tokens == 120 and r.completion_tokens == 30 for r in rows)

    # The LITERAL, not the constant. Every assertion above resolves the purpose
    # through `RUBRIC_GRADER_PURPOSE`, so redefining the constant to
    # `"assistant.turn"` — the orchestrator's own purpose — left all 27 tests
    # green while making "what did self-grading cost this project" unanswerable,
    # because grader rows and answer rows would share a label.
    assert RUBRIC_GRADER_PURPOSE == "assistant.rubric.grader", (
        "the grader's purpose is how its spend is separated from the "
        "orchestrator's. Changing it is allowed; changing it silently is not."
    )
    assert RUBRIC_GRADER_PURPOSE != "assistant.turn"

    # The MODEL, and therefore the price. `copilot_cost_callback` prices against
    # `ModelCall.model`, so a wrong name there yields `pricing_source='unknown'`
    # and `cost_usd=0` on every grader row — literally "self-grading is free",
    # the one statement this whole module exists to prevent. Setting the scope's
    # model to a name nobody prices left all 27 tests green.
    #
    # Compared against what `grader_model` resolved DURING the turn, not against
    # a name rebuilt afterwards: the claim under test is "the row names the
    # model that ran". And pinned to the parametrised literal as well, because
    # `turn.grader_model` is whatever the JUDGE binding in force resolved to —
    # satisfied by ANY binding, including one this module never chose, which is
    # precisely how these tests came to pass only after `test_route_smoke.py`
    # had leaked the `aleph-dev` template into the process-global `_runtime`.
    assert turn.grader_model == judge_model, (
        f"the grader resolved {turn.grader_model!r}; this test bound "
        f"{judge_model!r}. A different id means the JUDGE binding came from "
        "somewhere else — another test file's leak, or an operator profile — "
        "and this file is order-dependent again."
    )

    for row in rows:
        assert row.model == turn.grader_model, (
            f"the grader row names {row.model!r}; the grader ran "
            f"{turn.grader_model!r}. Pricing resolves off this field, so a "
            "wrong name prices the call at zero and reports self-grading as "
            "free."
        )
        # The orchestrator's model, specifically. A scope that falls through to
        # the enclosing call's model is the shape this most plausibly breaks in.
        assert row.model != "fake-chat", (
            "the grader row is attributed to the ORCHESTRATOR's model, so "
            "grader spend is priced as if the answering model produced it"
        )


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
    turn = await _drive_graded_turn(
        maker=maker,
        project_id=committed_project,
        run_id=uuid.uuid4(),
        monkeypatch=monkeypatch,
        files={},
    )
    rows = await _grader_rows(maker, committed_project)

    assert turn.upstream == 1, "an ungraded turn is one upstream request"
    assert rows == []
