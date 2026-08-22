"""Unit tests for the agent LLM cost-attribution callback (Wave 6 C1).

These run with `pytest -m "not integration"` — no compose stack. We feed
fake LangChain run lifecycle events to `AgentCostCallbackHandler` and assert
it records exactly one ModelCall + CostLedgerEvent via CostWriter.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from aleph_api.copilot_cost_callback import AgentCostCallbackHandler
from aleph_models.pricing import ModelPricing, PricingTable

MODEL = "claude-sonnet-4-6"


def _priced() -> PricingTable:
    """A table that knows `MODEL`.

    These tests used to pass `PricingTable()` and assert `cost_usd > 0`, which
    worked only because the module shipped a built-in price list containing
    this name. That list has been removed — it was wrong in every entry against
    the real gateway — so a table with no rates now correctly yields $0, and
    the test has to supply the rates it is asserting about.
    """
    return PricingTable(
        {
            MODEL: ModelPricing(
                input_per_token=Decimal("0.000003"),
                output_per_token=Decimal("0.000015"),
            )
        }
    )


PROJECT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "test-project")


class _FakeSession:
    """Captures `session.add(...)` rows; no-op flush/commit/context-manager."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _FakeSession:
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
        pricing=_priced(),
        model=MODEL,
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
    # Provenance travels with the row, so a zero can always be told apart from
    # an unpriced one.
    assert call.pricing_source == "static"
    assert call.input_rate_usd == Decimal("0.000003")


async def test_unpriceable_model_is_recorded_as_unknown_not_as_free() -> None:
    """The failure that made a broken pricing table invisible.

    An unrecognised model used to cost $0 with no trace, so a ledger full of
    them read as a cheap day rather than a misconfiguration. The call must
    still be recorded — losing it would be worse — but it must say so.
    """
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=PricingTable(),  # knows nothing
        model="a-model-nobody-priced",
    )
    run_id = uuid.uuid4()
    await handler.on_chat_model_start(
        serialized={}, messages=[[]], run_id=run_id, metadata={"projectId": str(PROJECT_ID)}
    )
    await handler.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="hi",
                            usage_metadata={
                                "input_tokens": 100000,
                                "output_tokens": 5000,
                                "total_tokens": 105000,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id=run_id,
    )

    from aleph_db.models.cost import ModelCall

    calls = [r for r in session.added if isinstance(r, ModelCall)]
    assert len(calls) == 1, "the call was dropped entirely; it must still be recorded"
    assert calls[0].pricing_source == "unknown", (
        "a call we could not price claimed a real pricing source — this is how "
        "100k Opus tokens get filed as $0.00 and nobody notices"
    )
    assert calls[0].cost_usd == 0


async def test_extracts_from_llm_output_token_usage_shape() -> None:
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=_priced(),
        model=MODEL,
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
        pricing=_priced(),
        model=MODEL,
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


def _settings_for_test() -> Any:
    """Minimal stand-in carrying only what `subagent_model` reads.

    `subagent_model` -> `_gateway_chat_model` reads the gateway address, the key,
    and the agent's request-timeout budget, so a SimpleNamespace suffices (no
    env / full Settings construction needed for this unit test).

    The timeout was a literal until the budget moved onto Settings, which is why
    it appears here at all: a stub that omits it now fails at construction,
    which is the correct and visible way for a stub to go stale.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        litellm_base_url="http://gateway.invalid/v1",
        insights_litellm_api_key="test-key",
        aleph_agent_request_timeout_s=180.0,
    )


def test_subagent_model_tags_purpose() -> None:
    from aleph_api.copilot_agent import subagent_model

    m = subagent_model(_settings_for_test(), "retriever")
    cbs = list(m.callbacks or [])
    purposes = [getattr(c, "_purpose", None) for c in cbs]
    assert "assistant.subagent.retriever" in purposes


async def test_no_usage_writes_an_unpriced_row_rather_than_nothing() -> None:
    """Replaces `test_skips_when_no_usage`, which pinned the defect.

    A response carrying no usage block — a provider that omits it, or
    `stream_usage` unset — used to produce NO record at all. An absent row and a
    free call are indistinguishable once they are both nothing, and the point of
    the ledger is that spend is never invisible. Zero tokens with a stated
    reason is a claim someone can check; an absence is not.
    """
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=_priced(),
        model=MODEL,
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

    calls = [obj for obj in session.added if type(obj).__name__ == "ModelCall"]
    assert len(calls) == 1, f"a usage-free response wrote {len(calls)} rows"
    assert calls[0].input_tokens == 0
    assert calls[0].completion_tokens == 0


async def test_a_failed_call_is_recorded_not_dropped() -> None:
    """`on_llm_error` popped the pending entry and recorded nothing.

    A provider that streams a partial response and then errors has already
    billed for what it produced, so dropping it made a failing model look free —
    the direction of error that hides a problem instead of surfacing it.
    """
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=_priced(),
        model=MODEL,
    )
    run_id = uuid.uuid4()
    await handler.on_chat_model_start(
        serialized={},
        messages=[[]],
        run_id=run_id,
        metadata={"projectId": str(PROJECT_ID)},
    )
    await handler.on_llm_error(RuntimeError("the gateway hung up"), run_id=run_id)

    calls = [obj for obj in session.added if type(obj).__name__ == "ModelCall"]
    assert len(calls) == 1, "a call that failed after starting was dropped"
    # Findable without joining anything: `purpose like '%.failed'`.
    assert calls[0].purpose.endswith(".failed")


async def test_an_error_with_no_project_scope_writes_nothing() -> None:
    """The error path must not invent a row it cannot attribute — the same rule
    the success path follows."""
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=_priced(),
        model=MODEL,
    )
    await handler.on_llm_error(RuntimeError("boom"), run_id=uuid.uuid4())
    assert session.added == []


async def test_the_run_id_comes_from_the_task_scope_not_from_metadata() -> None:
    """LangChain does not merge `configurable` into callback metadata.

    Verified: `ensure_config({"configurable": {...}})["metadata"]` is `{}`. So
    `metadata["agent_run_id"]` was never populated by anything, and the column
    was NULL for the life of the feature — not a bug in the reader, a channel
    that does not carry the key. `AlephAgentMiddleware` publishes a task-local
    scope around the call instead.
    """
    from aleph_api.chat_runs import ModelCallScope, model_call_scope

    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=_priced(),
        model=MODEL,
    )
    run_id = uuid.uuid4()
    agent_run_id = uuid.uuid4()
    with model_call_scope(ModelCallScope(agent_run_id=agent_run_id, model="answering-model")):
        await handler.on_chat_model_start(
            serialized={},
            messages=[[]],
            run_id=run_id,
            metadata={"projectId": str(PROJECT_ID)},
        )
        message = AIMessage(
            content="hi",
            usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        )
        await handler.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id
        )

    calls = [obj for obj in session.added if type(obj).__name__ == "ModelCall"]
    assert len(calls) == 1
    assert calls[0].agent_run_id == agent_run_id, "the run id did not reach the row"
    # And the model that ANSWERED, not the one resolved when the graph was built.
    assert calls[0].model == "answering-model"


# ---------------------------------------------------------------------------
# WS-MEP-1 — the agent path had no price list at all
#
# `_resolve_pricing` FABRICATED an empty `PricingTable()` and memoised it. So
# every model call on the agent path recorded `pricing_source="unknown"` — 100%
# of assistant traffic, the most expensive traffic in the system — and even once
# the gateway came up and discovery filled the real table, this handler kept its
# empty copy for the life of the process.
#
# The backlog blamed the gateway for not reporting rates. That is false:
# `claude-sonnet-4-6` is priced in the shipped hints file and `apply_hints`
# fills it. The rates existed; the agent could not see them.
# ---------------------------------------------------------------------------


def _production_shaped_handler() -> AgentCostCallbackHandler:
    """Exactly how `_gateway_chat_model` builds it: no `pricing=` argument.

    A test that passes `pricing=` proves the handler can use a table it is
    handed, which was never in doubt. The defect was that production hands it
    none.
    """
    return AgentCostCallbackHandler(model=MODEL, purpose="assistant.turn")


def test_a_handler_built_the_production_way_finds_the_bound_table() -> None:
    from aleph_api.copilot_agent import _runtime
    from aleph_models.pricing import PricingTable

    bound = PricingTable()
    previous = _runtime.get("pricing")
    _runtime["pricing"] = bound
    try:
        handler = _production_shaped_handler()
        # Object IDENTITY, not equality: `refresh_pricing` merges into the bound
        # table in place, so anything that copied it would be frozen at whatever
        # discovery had found at boot.
        assert handler._resolve_pricing() is bound
    finally:
        if previous is None:
            _runtime.pop("pricing", None)
        else:
            _runtime["pricing"] = previous


def test_an_unbound_table_is_not_cached_so_a_late_gateway_still_prices() -> None:
    """The memoised miss is the half that made this permanent.

    A gateway that was down at boot must produce priced calls once it comes up,
    with no restart — so a resolve that found nothing must not be remembered.
    """
    from aleph_api.copilot_agent import _runtime
    from aleph_models.pricing import PricingTable

    previous = _runtime.get("pricing")
    _runtime.pop("pricing", None)
    try:
        handler = _production_shaped_handler()
        first = handler._resolve_pricing()
        assert first.models() == []

        late = PricingTable()
        _runtime["pricing"] = late
        assert handler._resolve_pricing() is late, (
            "the empty table was memoised — the gateway coming up cannot help"
        )
    finally:
        if previous is None:
            _runtime.pop("pricing", None)
        else:
            _runtime["pricing"] = previous


def test_no_pricing_table_bound_is_reported_as_its_own_failure() -> None:
    """ "No table is bound" is a wiring bug; "this model is absent from the
    table" is a discovery gap. They were indistinguishable and need different
    fixes."""
    from structlog.testing import capture_logs

    from aleph_api.copilot_agent import _runtime

    previous = _runtime.get("pricing")
    _runtime.pop("pricing", None)
    try:
        with capture_logs() as captured:
            _production_shaped_handler()._resolve_pricing()
        events = [entry.get("event") for entry in captured]
        assert "agent.cost.no_pricing_table_bound" in events, captured
    finally:
        if previous is not None:
            _runtime["pricing"] = previous


def test_the_callback_no_longer_invents_a_table() -> None:
    """Pinned as a property of the source, because the alternative rename —
    `_empty_table()` — would satisfy a grep while changing nothing."""
    import ast
    import pathlib

    source = pathlib.Path("apps/api/src/aleph_api/copilot_cost_callback.py").read_text()
    tree = ast.parse(source)
    resolvers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_pricing"
    ]
    assert resolvers, "_resolve_pricing is gone — update this test"
    body = ast.dump(resolvers[0])
    # Constructing an empty table as a FALLBACK is fine; caching it is not.
    assert "self._pricing" not in body.split("PricingTable()")[-1], (
        "the empty fallback table is memoised again"
    )


def test_cache_write_tokens_are_extracted() -> None:
    """A column nothing wrote, against a rate the pricing table models at 1.25x.

    Omitting cache writes made every FIRST call in a cached conversation
    under-report — the opposite failure to the one caching exists to fix, and
    invisible because the reported number simply gets smaller.
    """
    from types import SimpleNamespace

    from aleph_api.copilot_cost_callback import _extract_usage

    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_token_details": {"cache_read": 200, "cache_creation": 300},
        }
    )
    response = SimpleNamespace(generations=[[SimpleNamespace(message=message)]], llm_output=None)

    usage = _extract_usage(response)  # type: ignore[arg-type]
    assert usage is not None
    input_tokens, cached, completion, cache_write = usage
    assert (input_tokens, cached, completion, cache_write) == (1000, 200, 50, 300)


def test_cache_write_tokens_cost_more_than_plain_input() -> None:
    """The premium is the point: modelling the discount without it is what made
    the numbers systematically wrong in the flattering direction."""
    from decimal import Decimal

    from aleph_models.pricing import ModelPricing, PricingTable

    table = PricingTable(
        {
            MODEL: ModelPricing(
                input_per_token=Decimal("0.000003"),
                output_per_token=Decimal("0.000015"),
                source="gateway",
            )
        }
    )
    plain = table.breakdown(model=MODEL, input_tokens=1000, cached_tokens=0, completion_tokens=0)
    written = table.breakdown(
        model=MODEL,
        input_tokens=1000,
        cached_tokens=0,
        completion_tokens=0,
        cache_write_tokens=1000,
    )
    assert written.cost_usd > plain.cost_usd


def _llm_result(*, input_tokens: int, output_tokens: int) -> Any:
    """The shape LangChain hands `on_llm_end`, with usage attached."""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="hi",
                        usage_metadata={
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": input_tokens + output_tokens,
                        },
                    )
                )
            ]
        ]
    )


async def test_the_recorded_model_is_the_one_that_answered() -> None:
    """`ModelCall.model` names the model the REQUEST used, not the one the
    handler was constructed with.

    The handler is built once, at boot, with whatever the profile bound then.
    A turn that resolves a different model — a capability override, a fallback
    after a failure, a profile switched mid-session — is still costed through
    that same handler, and pricing resolves off this exact field. Recording the
    construction-time name would price every such call against the wrong model:
    a recorded fact that is quietly wrong, which is worse than an absent one.

    The scope is set by `awrap_model_call`, which runs in the same task as the
    callback it wraps; `current_model_call_scope()` is the bridge.
    """
    from aleph_api.chat_runs import ModelCallScope, model_call_scope

    answered = "some-other-model-the-profile-did-not-bind"
    assert answered != MODEL, "the fixture must differ from the boot model"

    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=_priced(),
        model=MODEL,
    )
    run_id = uuid.uuid4()
    with model_call_scope(ModelCallScope(agent_run_id=None, model=answered)):
        await handler.on_chat_model_start(
            serialized={},
            messages=[[]],
            run_id=run_id,
            metadata={"projectId": str(PROJECT_ID)},
        )
        await handler.on_llm_end(_llm_result(input_tokens=10, output_tokens=5), run_id=run_id)

    calls = [obj for obj in session.added if type(obj).__name__ == "ModelCall"]
    assert len(calls) == 1
    assert calls[0].model == answered, (
        f"the row names {calls[0].model!r}, the model bound at BOOT, not "
        f"{answered!r}, the model that actually answered — so its cost is "
        "priced against the wrong rate card"
    )


async def test_with_no_scope_the_construction_time_model_is_used() -> None:
    """The fallback, so the fix above cannot be "always write None".

    Not every call arrives through `awrap_model_call` — the retrieval router
    makes three of its own a layer below the middleware — and a row with no
    model at all cannot be priced by anything.
    """
    session = _FakeSession()
    handler = AgentCostCallbackHandler(
        session_maker=_FakeSessionMaker(session),
        pricing=_priced(),
        model=MODEL,
    )
    run_id = uuid.uuid4()
    await handler.on_chat_model_start(
        serialized={},
        messages=[[]],
        run_id=run_id,
        metadata={"projectId": str(PROJECT_ID)},
    )
    await handler.on_llm_end(_llm_result(input_tokens=10, output_tokens=5), run_id=run_id)

    calls = [obj for obj in session.added if type(obj).__name__ == "ModelCall"]
    assert calls[0].model == MODEL
