"""Self-grading, measured rather than asserted (`WS-H3`).

Every test here drives a real compiled deepagents graph carrying the real
`build_grading_middleware(...)` list, with a real `RubricMiddleware` and a real
grader sub-agent built by `create_agent(response_format=GraderResponse)`. The
only doubles are the two chat models, whose replies are scripted so the models'
decisions are fixed and the only variable left is what the grading step does.
Nothing here stubs a library internal: the grader's verdict arrives the way a
gateway would deliver it, as a tool call the structured-output strategy binds.

Two of these encode failures that are invisible from outside:

  * The rubric source running *after* the grader does not stop grading; it
    breaks the iteration budget so grading dies silently a few turns later —
    `test_the_grader_never_sees_a_rubric_when_the_source_runs_late`.
  * Hitting the cap leaves the agent's last message untouched, so a give-up and
    a success are byte-identical in the transcript —
    `test_max_iterations_terminates_and_reports`.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from aleph_api.rubric import (
    DEFAULT_MAX_ITERATIONS,
    MAX_RUBRIC_CHARS,
    RUBRIC_GRADER_PURPOSE,
    RUBRIC_PATH,
    CostedRubricMiddleware,
    FrontendHandoffGuard,
    ProjectRubricMiddleware,
    build_grading_middleware,
    read_rubric,
)

RUBRIC = "1. Cite a source for every claim.\n2. Answer in under 200 words."

#: The tool the structured-output strategy binds for `response_format=GraderResponse`.
#: Discovered by binding it, not assumed: `create_agent` names the tool after the
#: pydantic model, and a wrong name here would make every scripted verdict
#: arrive as an unparsed tool call and every grading test pass as `grader_error`.
_GRADER_TOOL = "GraderResponse"


# ---------------------------------------------------------------------------
# Doubles: a scripted model and a backend holding one file.
# ---------------------------------------------------------------------------


class ScriptedModel(BaseChatModel):
    """A chat model whose replies are a fixed script, counting its own calls.

    One `_agenerate` is one upstream chat completion, which is the unit
    `scripts/_acceptance/agent_turn_probe.py` reports from the ledger. Counting
    here therefore measures the same quantity that probe does, deterministically
    and with no gateway.
    """

    replies: list[BaseMessage] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        # `create_agent` binds tools even when the script never calls one; a
        # model that raises NotImplementedError here (GenericFakeChatModel does)
        # cannot be used to drive a real graph at all.
        return self.bind(tools=tools, **kwargs)

    def _next(self, messages: list[BaseMessage]) -> ChatResult:
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        if not self.replies:
            msg = "ScriptedModel was given no replies"
            raise AssertionError(msg)
        return ChatResult(generations=[ChatGeneration(message=self.replies[index])])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._next(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._next(messages)


def _answer(text: str) -> AIMessage:
    return AIMessage(content=text)


def _verdict(result: str, *, gap: str | None = None) -> AIMessage:
    """A grader reply shaped the way the structured-output strategy expects one."""
    criteria: list[dict[str, Any]] = (
        [{"name": "cites a source", "passed": False, "gap": gap}]
        if gap is not None
        else [{"name": "cites a source", "passed": True}]
    )
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": _GRADER_TOOL,
                "args": {
                    "result": result,
                    "explanation": gap or "every criterion passes",
                    "criteria": criteria,
                },
                "id": f"grader-{result}-{len(criteria)}",
                "type": "tool_call",
            }
        ],
    )


class _Read:
    """The shape `BackendProtocol.aread` returns."""

    def __init__(self, *, error: str | None = None, content: str | None = None) -> None:
        self.error = error
        self.file_data = None if content is None else {"content": content}


class FileBackend:
    """A backend holding a fixed set of paths, and nothing else.

    Stands in for the production `CompositeBackend` at exactly the seam
    `ProjectRubricMiddleware` uses — one `aread(path)` call. The middleware is
    given the *factory*, as production gives it `_memory_backend`, so the test
    also covers the factory call itself.
    """

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.reads: list[str] = []

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> _Read:
        self.reads.append(file_path)
        if file_path not in self.files:
            return _Read(error=f"File '{file_path}' not found")
        return _Read(content=self.files[file_path])


def _backend_factory(files: dict[str, str]) -> Any:
    backend = FileBackend(files)
    return lambda _rt: backend


def _settings() -> Any:
    """Settings good enough to build a gateway model that is never called."""
    from aleph_api.settings import Settings

    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://aleph:x@localhost:5432/aleph",
        redis_url="redis://localhost:6379/0",
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        otel_exporter_otlp_endpoint="http://localhost:4317",
        litellm_base_url="http://localhost:18999",
        insights_litellm_api_key="unit-test-key-0123456789abcdef0123456789abcdef",
        aleph_agent_token_secret="unit-test-secret-0123456789abcdef0123456789abcdef",
        aleph_credential_master_key="c" * 64,
    )


class Turn:
    """One drive of a graded graph, and everything worth asserting about it."""

    def __init__(self, state: dict[str, Any], agent_calls: int, grader_calls: int) -> None:
        self.state = state
        self.agent_calls = agent_calls
        self.grader_calls = grader_calls

    @property
    def upstream_requests(self) -> int:
        """Chat completions this turn issued — the number the probe reports."""
        return self.agent_calls + self.grader_calls

    @property
    def last_text(self) -> str:
        return str(self.state["messages"][-1].content)


async def _drive(
    *,
    files: dict[str, str],
    agent_replies: list[BaseMessage],
    grader_replies: list[BaseMessage] | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    reverse_order: bool = False,
    thread: str = "t1",
) -> Turn:
    """Compile a real deep agent carrying the real grading middleware, and run it."""
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import MemorySaver

    agent_model = ScriptedModel(replies=agent_replies)
    grader = ScriptedModel(replies=grader_replies or [_verdict("satisfied")])

    middleware = build_grading_middleware(
        settings=_settings(),
        backend_factory=_backend_factory(files),
        max_iterations=max_iterations,
        model=grader,
    )
    if reverse_order:
        # The mutation criterion (a), expressed in the test rather than by hand
        # editing the module: the pair is returned ordered so a call site cannot
        # get it wrong, so the only way to reverse it is here.
        middleware = list(reversed(middleware))

    agent = create_deep_agent(
        model=agent_model,
        tools=[],
        middleware=middleware,
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": f"proj:11111111-1111-1111-1111-111111111111:{thread}"}}
    out = await agent.ainvoke({"messages": [{"role": "user", "content": "answer me"}]}, config)
    state = agent.get_state(config).values
    return Turn(
        dict(state) | {"messages": out["messages"]}, len(agent_model.calls), len(grader.calls)
    )


# ---------------------------------------------------------------------------
# The rubric reaches the graph, server-side.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_configured_rubric_lands_on_state() -> None:
    """FAILS TODAY without this module: nothing puts `rubric` on the run.

    The three assertions are one claim each and none of them is redundant.
    `rubric` proves the source middleware wrote the channel; `_active_rubric`
    and `_current_grading_run_id` prove `RubricMiddleware.abefore_agent` *saw*
    it — which is the half that silently stops being true if the two are
    reordered.
    """
    turn = await _drive(files={RUBRIC_PATH: RUBRIC}, agent_replies=[_answer("here you go")])

    assert turn.state["rubric"] == RUBRIC
    assert turn.state["_active_rubric"] == RUBRIC
    assert turn.state["_current_grading_run_id"]


@pytest.mark.asyncio
async def test_the_grader_never_sees_a_rubric_when_the_source_runs_late() -> None:
    """Mutation (a), pinned as a test rather than left to a hand edit.

    Reversed, grading does not *stop* — which is what makes it dangerous. The
    grader still runs, but `_active_rubric` is never set, so
    `_reset_for_new_rubric` never fires and `_rubric_iterations` never resets
    between turns. After `max_iterations` grader calls on the thread, every
    later turn terminates at `max_iterations_reached` on its first evaluation.
    """
    turn = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer("here you go")],
        reverse_order=True,
    )

    assert turn.state["rubric"] == RUBRIC, "the source still writes the key"
    assert turn.state.get("_active_rubric") is None, (
        "the grader's own before_agent ran first and saw no rubric — the bookkeeping "
        "reset never happens, and the iteration budget leaks across turns"
    )
    assert not turn.state.get("_current_grading_run_id")


@pytest.mark.asyncio
async def test_the_source_is_ordered_ahead_of_the_grader() -> None:
    """The list is the contract; the call site only splats it.

    Three, and the order of all three is load-bearing in opposite directions:
    `before_*` hooks run in list order (so the source is first) and `after_*`
    hooks run in reverse (so the handoff guard is first).
    """
    built = build_grading_middleware(settings=_settings(), model=ScriptedModel(replies=[]))

    assert [type(m) for m in built] == [
        ProjectRubricMiddleware,
        CostedRubricMiddleware,
        FrontendHandoffGuard,
    ]
    from deepagents.middleware.rubric import RubricMiddleware

    assert isinstance(built[1], RubricMiddleware), "the grader is still the library's"


# ---------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_criterion_triggers_one_revision() -> None:
    """One `needs_revision`, then `satisfied`: two agent turns, one extra grade."""
    turn = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer("no sources"), _answer("with sources [1]")],
        grader_replies=[_verdict("needs_revision", gap="no citation"), _verdict("satisfied")],
        max_iterations=DEFAULT_MAX_ITERATIONS,
    )

    assert turn.agent_calls == 2, "the revision is a whole extra agent turn"
    assert turn.grader_calls == 2
    assert turn.state["_rubric_status"] == "satisfied"
    assert turn.last_text == "with sources [1]", "the revised answer is what is handed over"


@pytest.mark.asyncio
async def test_the_revision_prompt_carries_the_failing_criterion() -> None:
    """A revision the model cannot act on is a slower way to give the same answer."""
    turn = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer("no sources"), _answer("with sources [1]")],
        grader_replies=[_verdict("needs_revision", gap="no citation"), _verdict("satisfied")],
    )

    injected = [
        m for m in turn.state["messages"] if m.additional_kwargs.get("lc_source") == "rubric_grader"
    ]
    assert injected, "the grader's feedback never reached the model"
    assert "no citation" in str(injected[0].content)


@pytest.mark.asyncio
async def test_max_iterations_terminates_and_reports(caplog: pytest.LogCaptureFixture) -> None:
    """Giving up must not read as success.

    `RubricMiddleware` deliberately leaves the response untouched on a
    non-satisfied termination (rubric.py:305-318), so the transcript alone
    cannot tell the two apart. `_rubric_status` and the library's own warning
    are the only two signals, and this asserts both.
    """
    caplog.set_level(logging.WARNING, logger="deepagents.middleware.rubric")
    turn = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer("attempt one"), _answer("attempt two"), _answer("attempt three")],
        grader_replies=[_verdict("needs_revision", gap="still no citation")],
        max_iterations=2,
    )

    assert turn.state["_rubric_status"] == "max_iterations_reached"
    assert turn.grader_calls == 2, "the cap bounds grader calls, not just revisions"
    assert any("max_iterations" in r.getMessage() for r in caplog.records), (
        "the library's warning is the only thing that says the agent gave up"
    )


@pytest.mark.asyncio
async def test_a_turn_issues_at_most_three_grader_calls() -> None:
    """`max_iterations=2` is a hard bound on grader calls, whatever the verdicts."""
    turn = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer(f"attempt {i}") for i in range(6)],
        grader_replies=[_verdict("needs_revision", gap="never satisfied")],
        max_iterations=2,
    )

    assert turn.grader_calls <= 3
    assert turn.grader_calls == 2


def test_max_iterations_over_the_library_cap_is_refused() -> None:
    """Mutation (b). The library's own guard, reached through Aleph's builder."""
    with pytest.raises(ValueError, match=r"max_iterations.*\[1, 20\]"):
        build_grading_middleware(
            settings=_settings(),
            model=ScriptedModel(replies=[]),
            max_iterations=21,
        )


# ---------------------------------------------------------------------------
# Inert with no rubric — the state every project is in until it writes one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_rubric_means_no_grader_call() -> None:
    """No file, no grader, and the same answer the ungraded graph produces."""
    graded = await _drive(files={}, agent_replies=[_answer("plain answer")])

    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import MemorySaver

    bare_model = ScriptedModel(replies=[_answer("plain answer")])
    bare = create_deep_agent(model=bare_model, tools=[], checkpointer=MemorySaver())
    out = await bare.ainvoke(
        {"messages": [{"role": "user", "content": "answer me"}]},
        {"configurable": {"thread_id": "proj:11111111-1111-1111-1111-111111111111:bare"}},
    )

    assert graded.grader_calls == 0
    assert graded.state.get("rubric") is None
    assert graded.agent_calls == len(bare_model.calls) == 1
    assert graded.last_text == str(out["messages"][-1].content)


@pytest.mark.asyncio
async def test_an_empty_rubric_file_is_the_same_as_no_rubric() -> None:
    """A project that emptied the file has turned grading off, not broken it."""
    turn = await _drive(files={RUBRIC_PATH: "   \n\n  "}, agent_replies=[_answer("plain")])

    assert turn.grader_calls == 0
    assert turn.state.get("rubric") is None


@pytest.mark.asyncio
async def test_a_backend_that_raises_does_not_kill_the_turn() -> None:
    """A store outage degrades to ungraded, which is the pre-H3 behaviour."""

    class Exploding:
        async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
            msg = "store unreachable"
            raise RuntimeError(msg)

    middleware = ProjectRubricMiddleware(backend_factory=lambda _rt: Exploding())
    update = await middleware.abefore_agent({"messages": []}, runtime=None)  # type: ignore[arg-type]

    assert update is None


@pytest.mark.asyncio
async def test_an_edited_rubric_takes_effect_on_the_next_turn() -> None:
    """A standard someone rewrites and nothing changes is worse than no standard.

    `rubric` lives on a checkpointed thread, so reading the file once and
    keeping it would leave an analyst editing `/memories/rubric.md` mid-
    conversation with no change and no explanation.
    """
    middleware = ProjectRubricMiddleware(backend_factory=_backend_factory({RUBRIC_PATH: RUBRIC}))
    update = await middleware.abefore_agent(
        {"messages": [], "rubric": "the previous turn's rubric"},  # type: ignore[typeddict-item]
        runtime=None,
    )

    assert update == {"rubric": RUBRIC}


@pytest.mark.asyncio
async def test_an_unreadable_file_does_not_blank_a_rubric_already_in_play() -> None:
    """A store outage must degrade to "grade against what we had", not to silence."""

    class Exploding:
        async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
            msg = "store unreachable"
            raise RuntimeError(msg)

    middleware = ProjectRubricMiddleware(backend_factory=lambda _rt: Exploding())
    update = await middleware.abefore_agent(
        {"messages": [], "rubric": RUBRIC},  # type: ignore[typeddict-item]
        runtime=None,
    )

    assert update is None, "None leaves the existing rubric alone; {'rubric': ''} would not"


@pytest.mark.asyncio
async def test_an_unchanged_rubric_is_not_rewritten_every_turn() -> None:
    """The common case writes nothing, so the update log says what actually moved."""
    middleware = ProjectRubricMiddleware(backend_factory=_backend_factory({RUBRIC_PATH: RUBRIC}))
    update = await middleware.abefore_agent(
        {"messages": [], "rubric": RUBRIC},  # type: ignore[typeddict-item]
        runtime=None,
    )

    assert update is None


@pytest.mark.asyncio
async def test_an_enormous_rubric_is_bounded() -> None:
    """It is interpolated into every grader prompt, so its length is multiplied."""
    backend = FileBackend({RUBRIC_PATH: "x" * (MAX_RUBRIC_CHARS * 3)})
    text = await read_rubric(backend)

    assert text is not None
    assert len(text) == MAX_RUBRIC_CHARS


@pytest.mark.asyncio
async def test_the_rubric_is_read_from_the_agents_own_memories_route() -> None:
    """The path is the contract with the writer — `write_file /memories/rubric.md`.

    `CompositeBackend` routes `/memories/` to the per-project `StoreBackend`, so
    reading through the backend factory is what makes this the same file the
    agent's own filesystem tools write. A path that drifted from that route
    would read nothing, forever, and report nothing.
    """
    backend = FileBackend({RUBRIC_PATH: RUBRIC})
    middleware = ProjectRubricMiddleware(backend_factory=lambda _rt: backend)
    await middleware.abefore_agent({"messages": []}, runtime=None)  # type: ignore[arg-type]

    assert backend.reads == ["/memories/rubric.md"]


# ---------------------------------------------------------------------------
# Attribution: the grader's spend has to join to the turn that caused it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_grader_call_runs_inside_the_turns_cost_scope() -> None:
    """The run id reaches the grader's model call, or the turn probe cannot see it.

    `scripts/_acceptance/agent_turn_probe.py` counts a turn's upstream requests
    by `agent_run_id`, and the run id travels in `config["configurable"]` — a
    channel LangChain does not merge into the callback `metadata` the cost
    handler reads. The grader is a separate agent invoked from `aafter_agent`,
    so it never passes through `AlephAgentMiddleware.awrap_model_call`, which is
    the only other place that bridge is published. Measured before the fix: the
    rows carried `agent_run_id = NULL`.
    """
    from aleph_api.chat_runs import current_model_call_scope

    seen: list[Any] = []

    class ScopeWatchingModel(ScriptedModel):
        def _next(self, messages: list[BaseMessage]) -> ChatResult:
            seen.append(current_model_call_scope())
            return super()._next(messages)

    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import MemorySaver

    run_id = "0f6bd9a2-1c2f-4d0a-9c1e-6b2a0e2f77aa"
    agent = create_deep_agent(
        model=ScriptedModel(replies=[_answer("done")]),
        tools=[],
        middleware=build_grading_middleware(
            settings=_settings(),
            backend_factory=_backend_factory({RUBRIC_PATH: RUBRIC}),
            model=ScopeWatchingModel(replies=[_verdict("satisfied")]),
        ),
        checkpointer=MemorySaver(),
    )
    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "answer me"}]},
        {
            "configurable": {
                "thread_id": "proj:11111111-1111-1111-1111-111111111111:scope",
                "agent_run_id": run_id,
            }
        },
    )

    assert seen, "the grader never ran"
    assert seen[0] is not None, "no cost scope was published around the grader call"
    assert str(seen[0].agent_run_id) == run_id


@pytest.mark.asyncio
async def test_the_cost_scope_does_not_outlive_the_grading_call() -> None:
    """A task-local identity that leaks would mislabel the next call, not fail."""
    from aleph_api.chat_runs import current_model_call_scope

    await _drive(files={RUBRIC_PATH: RUBRIC}, agent_replies=[_answer("done")], thread="leak")

    assert current_model_call_scope() is None


# ---------------------------------------------------------------------------
# The browser handoff, which looks exactly like a finished turn.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("copilotkit_first", [False, True])
@pytest.mark.asyncio
async def test_a_turn_handing_a_tool_call_to_the_browser_is_not_graded(
    copilotkit_first: bool,
) -> None:
    """`open_page` must reach the browser, not be revised into another paragraph.

    Driven through the real `CopilotKitMiddleware`, not a simulated state key:
    `after_model` strips the frontend call off the AI message and parks it in
    `copilotkit.intercepted_tool_calls`, which leaves the turn looking finished
    to every `after_agent` hook. That is exactly the state the guard exists for,
    and building it by hand would prove nothing about the real interception.
    """
    from copilotkit import CopilotKitMiddleware
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import MemorySaver

    handoff = AIMessage(
        content="opening it",
        tool_calls=[
            {"name": "open_page", "args": {"page_id": "p1"}, "id": "fe1", "type": "tool_call"}
        ],
    )
    agent_model = ScriptedModel(replies=[handoff])
    grader = ScriptedModel(replies=[_verdict("needs_revision", gap="you answered nothing")])

    grading = build_grading_middleware(
        settings=_settings(),
        backend_factory=_backend_factory({RUBRIC_PATH: RUBRIC}),
        model=grader,
    )
    order = (
        [CopilotKitMiddleware(), *grading]
        if copilotkit_first
        else [*grading, CopilotKitMiddleware()]
    )
    agent = create_deep_agent(
        model=agent_model,
        tools=[],
        middleware=order,
        checkpointer=MemorySaver(),
    )
    config = {
        "configurable": {
            "thread_id": f"proj:11111111-1111-1111-1111-111111111111:handoff-{copilotkit_first}"
        }
    }
    out = await agent.ainvoke(
        {
            "messages": [{"role": "user", "content": "open page one"}],
            "copilotkit": {"actions": [{"name": "open_page"}]},
        },
        config,
    )

    assert len(grader.calls) == 0, "the grader ran on a turn that had not finished"
    assert len(agent_model.calls) == 1, "the run was resumed instead of handing off"
    restored = out["messages"][-1]
    assert [c["name"] for c in restored.tool_calls] == ["open_page"], (
        "CopilotKitMiddleware must still get to restore the intercepted call"
    )


@pytest.mark.asyncio
async def test_the_guard_only_vetoes_the_handoff_turn() -> None:
    """A veto that fired on every turn would be indistinguishable from no rubric."""
    guard = FrontendHandoffGuard()

    graded = await guard.aafter_agent({"messages": [], "rubric": RUBRIC}, runtime=None)  # type: ignore[typeddict-item]
    vetoed = await guard.aafter_agent(
        {
            "messages": [],
            "rubric": RUBRIC,
            "copilotkit": {"intercepted_tool_calls": [{"name": "focus_tab"}]},
        },  # type: ignore[typeddict-item]
        runtime=None,
    )

    assert graded is None
    assert vetoed == {"rubric": ""}


# ---------------------------------------------------------------------------
# The real orchestrator graph, and the producer/consumer handshake.
# ---------------------------------------------------------------------------


def _production_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The kwargs the real builder hands `create_deep_agent`.

    Same idiom as `test_interpreter_middleware.py` and `test_agent_skill_wiring.py`:
    reading a constant would stay green while somebody deleted the call site,
    and the call site is the feature. Here it also supplies the REAL
    `_memory_backend` factory, which is the thing this file cannot fake — the
    `/memories/` route, the per-project namespace and the store key are all
    defined in `copilot_agent`, and `rubric.py` reads through them rather than
    restating them.
    """
    import deepagents

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", _capture)
    from aleph_api.copilot_agent import build_assistant_deep_agent

    build_assistant_deep_agent(settings=_settings(), store=None)  # ty: ignore[invalid-argument-type]
    # Put the real builder back BEFORE the test body compiles anything, or the
    # graph under test is a bare `object()` and every assertion passes vacuously.
    monkeypatch.undo()
    return captured


@pytest.mark.asyncio
async def test_the_agent_writes_the_rubric_and_the_next_turn_grades_against_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The producer and the consumer, proven to agree on one path.

    This is the only test that touches neither a fake backend nor a guessed
    store key. Turn one has the model call the real `write_file` tool at
    `/memories/rubric.md`; turn two reads it back through
    `ProjectRubricMiddleware`. Between those two points sit the production
    `CompositeBackend`, its `/memories/` route, `_memory_namespace`'s
    per-project scoping and `StoreBackend`'s key format — none of which this
    module owns, and any of which could drift.

    Without it, `RUBRIC_PATH` would be a claim about somebody else's routing
    table, and a wrong claim there fails by reading nothing forever.

    It takes the PRODUCTION middleware list as-is and swaps only the two models.
    It used to append `build_grading_middleware(...)` to that list, which worked
    only while the feature was unwired: once the real builder included it,
    deepagents refused the graph with "Please remove duplicate middleware
    instances." A test that goes red the moment its subject ships is structurally
    dependent on the subject not shipping, and this one guarded the single most
    important thing in the workstream.
    """
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.store.memory import InMemoryStore

    kwargs = _production_kwargs(monkeypatch)
    # The production `AlephAgentMiddleware` reads `get_settings()` inside
    # `awrap_model_call`, which builds `Settings()` from the environment — there
    # is none here. Patched AFTER `_production_kwargs`, whose own `monkeypatch.undo()`
    # would otherwise take this with it.
    monkeypatch.setattr("aleph_api.settings.get_settings", _settings)
    store = InMemoryStore()

    write = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": RUBRIC_PATH, "content": RUBRIC},
                "id": "w1",
                "type": "tool_call",
            }
        ],
    )
    agent_model = ScriptedModel(replies=[write, _answer("saved"), _answer("graded answer")])
    grader = ScriptedModel(replies=[_verdict("satisfied")])

    # The production list already carries the two grading middlewares, in the
    # order `build_grading_middleware` fixed. Only the grader's MODEL is
    # swapped, so the routing under test is production's and the verdict is
    # scripted.
    production_middleware = list(kwargs["middleware"])
    graders = [m for m in production_middleware if isinstance(m, CostedRubricMiddleware)]
    assert len(graders) == 1, (
        "the production agent does not carry exactly one CostedRubricMiddleware "
        f"(found {len(graders)}). If it carries none, `build_grading_middleware` "
        "has been unwired from `build_assistant_deep_agent` and self-grading is "
        f"off. Middleware: {[type(m).__name__ for m in production_middleware]}"
    )
    sources = [m for m in production_middleware if isinstance(m, ProjectRubricMiddleware)]
    assert len(sources) == 1, "the rubric source is missing from the production agent"
    assert production_middleware.index(sources[0]) < production_middleware.index(graders[0]), (
        "the grader runs before the rubric source. This does not fail loudly — it "
        "degrades into a grading loop whose iteration budget never resets."
    )
    # Swap the grader instance for one with the scripted model, IN PLACE, so
    # the position `build_grading_middleware` chose is preserved. Rebuilt rather
    # than having its private `_model` reassigned: the attribute belongs to the
    # library's `RubricMiddleware`, and a test that reaches into it would keep
    # passing after an upstream rename while grading silently used the real
    # gateway model.
    rebuilt = build_grading_middleware(
        settings=_settings(),
        # The production factory, not a stand-in. This is the whole point.
        backend_factory=kwargs["backend"],
        model=grader,
    )
    scripted_grader = next(m for m in rebuilt if isinstance(m, CostedRubricMiddleware))
    production_middleware[production_middleware.index(graders[0])] = scripted_grader

    agent = create_deep_agent(
        **{
            **kwargs,
            "model": agent_model,
            "store": store,
            "checkpointer": MemorySaver(),
            "middleware": production_middleware,
        }
    )
    project = "proj:11111111-1111-1111-1111-111111111111"
    # Two DIFFERENT threads on one project, which is what makes the route choice
    # testable rather than decorative. `/memories/` is the only prefix
    # `CompositeBackend` sends to a `StoreBackend`; everything else falls
    # through to the per-thread, in-state `StateBackend`, where a rubric written
    # in one conversation is simply not there in the next one. Same thread for
    # both turns would pass against either route.
    wrote = {"configurable": {"thread_id": f"{project}:conversation-one"}}
    later = {"configurable": {"thread_id": f"{project}:conversation-two"}}

    first = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "remember this rubric"}]}, wrote
    )
    assert any(getattr(m, "name", None) == "write_file" for m in first["messages"]), (
        "the rubric was never written; the rest of this test would pass vacuously"
    )
    assert len(grader.calls) == 0, "turn one had no rubric yet"

    await agent.ainvoke({"messages": [{"role": "user", "content": "now answer"}]}, later)
    state = agent.get_state(later).values

    assert state["rubric"] == RUBRIC, (
        "the agent wrote the rubric in one conversation and the next one could not "
        "see it — RUBRIC_PATH is not on a durable, per-project route"
    )
    assert state["_rubric_status"] == "satisfied"
    assert len(grader.calls) == 1


# ---------------------------------------------------------------------------
# Cost. The number the plan has to be able to argue with.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grading_costs_this_many_upstream_requests(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What self-grading adds, as a counted number rather than an estimate.

    One `_agenerate` is one upstream chat completion, which is exactly what
    `scripts/_acceptance/agent_turn_probe.py` counts from `model_calls` — the
    grader's calls are attributed to the same run, because `agent_run_id` rides
    `configurable` and `ensure_config` copies it into the metadata the cost
    callback reads. So these numbers and the probe's are the same quantity.
    """
    ungraded = await _drive(files={}, agent_replies=[_answer("plain")], thread="cost-off")
    passed = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer("good")],
        grader_replies=[_verdict("satisfied")],
        thread="cost-pass",
    )
    revised = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer("bad"), _answer("good")],
        grader_replies=[_verdict("needs_revision", gap="no citation"), _verdict("satisfied")],
        thread="cost-revise",
    )
    capped = await _drive(
        files={RUBRIC_PATH: RUBRIC},
        agent_replies=[_answer(f"try {i}") for i in range(5)],
        grader_replies=[_verdict("needs_revision", gap="never")],
        thread="cost-cap",
    )

    print(
        "\nWS-H3 upstream chat completions for one no-tool turn "
        f"(max_iterations={DEFAULT_MAX_ITERATIONS}):"
        f"\n  no rubric (inert)        {ungraded.upstream_requests}"
        f"\n  graded, satisfied first  {passed.upstream_requests}"
        f"\n  graded, one revision     {revised.upstream_requests}"
        f"\n  graded, cap reached      {capped.upstream_requests}   <- worst case"
    )
    _ = capsys  # printed for the record; the assertions below are the gate.

    assert ungraded.upstream_requests == 1
    assert passed.upstream_requests == 2, "grading a turn nobody revises DOUBLES it"
    assert revised.upstream_requests == 4
    assert capped.upstream_requests == 4, "the cap bounds the worst case at 4x a plain turn"


def test_the_grader_model_is_cost_attributed() -> None:
    """The grader is the one model that could have called the gateway uncounted.

    Same evidence shape as `test_subagents.py`: building the model constructs a
    gateway-pointed `ChatOpenAI` and never calls it, so the attached
    `AgentCostCallbackHandler`'s purpose is the proof of the cost tag.
    """
    from langchain_openai import ChatOpenAI

    from aleph_api.copilot_cost_callback import AgentCostCallbackHandler
    from aleph_api.rubric import grader_model

    model = grader_model(_settings())

    assert isinstance(model, ChatOpenAI)
    handlers = [c for c in (model.callbacks or []) if isinstance(c, AgentCostCallbackHandler)]
    assert len(handlers) == 1
    assert handlers[0]._purpose == RUBRIC_GRADER_PURPOSE
    assert str(model.openai_api_base).endswith("/v1")
