"""The agent's scratchpad, measured rather than asserted (`WS-H5`).

Every test here drives the real `CodeInterpreterMiddleware` through a real
compiled deepagents graph against `aleph_models.testing.FakeGateway`, with the
gateway's replies scripted so the model's decisions are fixed and the only
variable left is what the interpreter actually does. No socket is opened and no
live gateway is needed, which matters more than speed: the claim under test is
"one eval call issues one upstream chat completion no matter how many items the
loop touches", and that is a claim about counted traffic.

Two of these encode defects measured in this repository's own venv, both of
which make the feature look like it works:

  * PTC drops the `RunnableConfig`, so every Aleph tool loses project scope and
    answers "unavailable" — `test_the_scratchpad_keeps_project_scope`.
  * A raising tool propagates out of `eval` and out of `ainvoke`, discarding
    every item the loop had already completed —
    `test_one_bad_item_does_not_lose_the_batch`.

`langchain-quickjs` is the only new runtime dependency `docs/plan.md` asks for,
and it is now pinned (`apps/api/pyproject.toml`, `langchain-quickjs~=0.2.0` on
`deepagents>=0.6.8,<0.7`; resolved to langchain-quickjs 0.2.0 + quickjs-rs 0.1.2
+ deepagents 0.6.12). These tests used to SKIP when it was absent, which is how
seven of the ten reported green for a feature the API could not even start with.
`_quickjs()` now asserts instead.
"""

from __future__ import annotations

import json
import threading
from typing import Any, NamedTuple

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from aleph_api.interpreter import (
    INTERPRETER_MAX_PTC_CALLS,
    INTERPRETER_TOOL_NAME,
    PTC_ALLOWLIST,
    PTC_WITHHELD,
    build_interpreter_middleware,
    guarded_ptc_tools,
)

_PIN = (
    "langchain-quickjs is missing from this environment. It is a pinned runtime "
    "dependency of aleph-api (`langchain-quickjs~=0.2.0`, which requires "
    "deepagents>=0.6.8 — both are in apps/api/pyproject.toml). Run "
    "`uv sync --all-packages --all-extras`."
)

_THREAD_ID = "proj:11111111-1111-1111-1111-111111111111:t1"


def _quickjs() -> None:
    """Fail — do not skip — when the interpreter is not installed.

    This was `pytest.importorskip` while the dependency was still a proposal,
    and it is the reason seven of the ten tests here reported green for a
    feature that could not run: `build_interpreter_middleware` raises without
    the package, so `build_assistant_deep_agent` cannot start the API, and the
    suite said `7 skipped` rather than saying that. Now that the pin is real, a
    missing package is a broken environment, and a broken environment must be a
    red rather than a quieter green.
    """
    import importlib.util

    assert importlib.util.find_spec("langchain_quickjs") is not None, _PIN


# ---------------------------------------------------------------------------
# The allowlist partition — no interpreter needed, so these never skip.
# ---------------------------------------------------------------------------


def _orchestrator_tools() -> list[BaseTool]:
    from aleph_api.copilot_agent import _ORCHESTRATOR_TOOLS

    return list(_ORCHESTRATOR_TOOLS)


def test_every_orchestrator_tool_is_classified() -> None:
    """A new chat tool must be a decision, not a default.

    Two-way, because each direction fails differently. A tool in neither list is
    silently unreachable from the scratchpad — the loop cannot call it and
    nothing says why. A name in a list that matches no tool is worse:
    `filter_tools_for_ptc` treats an unmatched name as "expose nothing", so a
    rename leaves the model with a `tools.*` namespace one entry short and no
    error anywhere.
    """
    live = {tool.name for tool in _orchestrator_tools()}
    classified = set(PTC_ALLOWLIST) | set(PTC_WITHHELD)

    assert live - classified == set(), (
        "orchestrator tools with no PTC decision: add each to PTC_ALLOWLIST "
        "(read-only, safe in a loop with no HITL gate) or PTC_WITHHELD with a reason"
    )
    assert classified - live == set(), "PTC lists name tools the orchestrator no longer carries"
    assert set(PTC_ALLOWLIST) & set(PTC_WITHHELD) == set(), "a tool is both exposed and withheld"


def test_no_mutating_tool_is_reachable_from_the_scratchpad() -> None:
    """PTC bypasses `interrupt_on`, so one approved eval is a blank cheque.

    Named literally rather than derived: the point is that somebody adding
    `set_model_profile` to the allowlist has to delete a line that says why it
    is not there.
    """
    for name in ("set_connector_enabled", "set_model_profile", "pin_to_brief", "spotlight"):
        assert name in PTC_WITHHELD, f"{name} mutates state and must not be callable in a loop"
        assert name not in PTC_ALLOWLIST


def test_an_allowlist_name_the_agent_does_not_carry_is_an_error() -> None:
    """The rename case, pinned. Silence here is the whole defect class."""
    with pytest.raises(ValueError, match="PTC_ALLOWLIST names"):
        guarded_ptc_tools([])


# ---------------------------------------------------------------------------
# Registration on the real graph.
# ---------------------------------------------------------------------------


@pytest.fixture
def production_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The kwargs the real builder hands `create_deep_agent`.

    Same idiom as `test_agent_skill_wiring.py`: reading the constant would stay
    green while somebody deleted the call site, and the call site is the feature.
    """
    import deepagents

    from aleph_api.settings import Settings

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", _capture)

    from aleph_api.copilot_agent import build_assistant_deep_agent

    build_assistant_deep_agent(settings=_settings_with_placeholders(Settings), store=None)
    # Put the real `create_deep_agent` back before the test body runs: the
    # registration assertion compiles the graph from these captured kwargs, and
    # a still-patched builder would hand it a bare object and pass vacuously.
    monkeypatch.undo()
    return captured


def _settings_with_placeholders(cls: type) -> Any:
    """Settings with every required secret filled by an obvious placeholder.

    Derived from the model rather than listed: the secret fields have no
    production default and the set grows, and a hard-coded list here would fail
    on an unrelated change. Nothing built from these settings reaches a gateway.
    """
    values = {
        name: "placeholder-not-a-real-secret-placeholder-not-a-real-secret-xxxxx"
        for name, field in cls.model_fields.items()
        if field.is_required()
    }
    return cls(**values)


def test_interpreter_tool_is_registered(production_kwargs: dict[str, Any]) -> None:
    """The interpreter is on the graph the assistant actually runs, and last.

    Two assertions because they fail differently. Ordering: middleware earlier
    in the list is OUTER, so the last entry is the one that sees the request the
    model will receive — and `request.tools` is what the interpreter filters the
    PTC allowlist against. Placed ahead of `CopilotKitMiddleware` it would
    install bridges for a toolset that later middleware then changes.
    Registration: the middleware contributes its `eval` tool to the compiled
    tool node, and that is what "the agent has a scratchpad" means.
    """
    _quickjs()
    from copilotkit import CopilotKitMiddleware
    from deepagents import create_deep_agent
    from langchain_quickjs import CodeInterpreterMiddleware

    middleware = list(production_kwargs["middleware"])
    kinds = [type(m).__name__ for m in middleware]

    interpreter_at = next(
        (i for i, m in enumerate(middleware) if isinstance(m, CodeInterpreterMiddleware)), None
    )
    assert interpreter_at is not None, (
        f"the orchestrator's middleware list is {kinds} — no CodeInterpreterMiddleware, "
        "so the agent has no scratchpad and falls back to guessing a fan-out width"
    )
    copilotkit_at = next(
        (i for i, m in enumerate(middleware) if isinstance(m, CopilotKitMiddleware)), None
    )
    assert copilotkit_at is not None, f"CopilotKitMiddleware disappeared from {kinds}"
    assert copilotkit_at < interpreter_at, (
        f"middleware order is {kinds}; the interpreter must come last so it filters "
        "the final tool list"
    )

    # Compile the graph from the production kwargs and read the tool node.
    graph = create_deep_agent(**production_kwargs)
    registered = set(graph.nodes["tools"].bound.tools_by_name)
    assert INTERPRETER_TOOL_NAME in registered, sorted(registered)


# ---------------------------------------------------------------------------
# Behaviour, against a scripted gateway.
# ---------------------------------------------------------------------------


def _eval_call(code: str) -> dict[str, Any]:
    """A chat completion whose only content is a call to the interpreter."""
    return {
        "id": "chatcmpl-eval",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-chat",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_eval_1",
                            "type": "function",
                            "function": {
                                "name": INTERPRETER_TOOL_NAME,
                                "arguments": json.dumps({"code": code}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _plain_answer(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-answer",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-chat",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Turn(NamedTuple):
    """What one scripted turn produced, and what it cost."""

    #: The `eval` tool message's text — what the loop returned to the model.
    text: str
    #: Upstream chat completions this turn issued. Scripted, so this measures
    #: the interpreter's cost and nothing else: a fan-out the model drove one
    #: item at a time would need one scripted reply per item.
    requests: int
    #: The graph's final state, for assertions about what got checkpointed.
    state: dict[str, Any]


async def _run_script(code: str, tools: list[BaseTool]) -> Turn:
    """Drive one turn that calls the interpreter once, and count the traffic."""
    from deepagents import create_deep_agent
    from langchain_openai import ChatOpenAI

    from aleph_models.testing import FakeGateway, GatewayConfig, ScriptedResponse

    fake = FakeGateway(
        GatewayConfig.well_behaved(
            invoke_script=(
                ScriptedResponse(status=200, body=_eval_call(code)),
                ScriptedResponse(status=200, body=_plain_answer("done")),
            )
        )
    )
    http = fake.client()
    try:
        model = ChatOpenAI(
            model="fake-chat",
            api_key="sk-fake-virtual-key",  # ty: ignore[invalid-argument-type]
            base_url=fake.base_url + "/v1",
            http_async_client=http,
            max_retries=0,
        )
        agent = create_deep_agent(
            model=model,
            tools=tools,
            middleware=build_interpreter_middleware(tools=tools),
        )
        out = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "cover the list"}]},
            config={"configurable": {"thread_id": _THREAD_ID}},
        )
    finally:
        await http.aclose()

    evals = [m for m in out["messages"] if getattr(m, "name", None) == INTERPRETER_TOOL_NAME]
    assert evals, "the scripted turn never reached the interpreter"
    return Turn(str(evals[0].content), fake.count("/v1/chat/completions"), dict(out))


def _toolset(**impls: Any) -> list[BaseTool]:
    """A stand-in for the orchestrator's toolset, one tool per allowlisted name.

    `guarded_ptc_tools` refuses a name it cannot resolve — deliberately; that is
    `test_an_allowlist_name_the_agent_does_not_carry_is_an_error`. So a fixture
    has to supply the whole allowlist rather than invent a name, which also
    means every test here runs against the real set of tools a loop may call.
    Pass `search_wiki=<coroutine>` to give one of them a body.
    """

    async def _stub(arg: str = "") -> str:
        """Stand-in for an allowlisted read-only orchestrator tool."""
        return "ok"

    built: list[BaseTool] = []
    for name in PTC_ALLOWLIST:
        made = tool(impls.get(name, _stub))
        made.name = name
        built.append(made)
    return built


def _camel(name: str) -> str:
    """`snake_case` → `camelCase`, the way the REPL names a bridged tool."""
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


@pytest.mark.asyncio
async def test_one_eval_covers_every_item_at_a_fixed_request_count() -> None:
    """20 of 20, for a request count that does not move when 20 becomes 200.

    This is the whole workstream in one assertion. The model-driven version of
    this task samples — it checks three items and answers as though it checked
    twenty — and issues an unpredictable number of upstream requests doing it.
    The loop covers the list, and the gateway sees the eval turn and the answer
    turn and nothing else.
    """
    _quickjs()
    touched: list[str] = []

    async def look_up(query: str, top_k: int = 6) -> str:
        """Scan the wiki for one query."""
        touched.append(query)
        return f"{query}: ok"

    fixture = _toolset(search_wiki=look_up)
    code = """
const items = Array.from({length: 20}, (_, i) => `item-${i}`);
const out = [];
for (const it of items) { out.push(await tools.searchWiki({query: it})); }
`covered ${out.length}/${items.length}`;
"""
    counts: list[int] = []
    for _ in range(3):
        touched.clear()
        turn = await _run_script(code, fixture)
        assert "covered 20/20" in turn.text, turn.text
        assert len(touched) == 20, f"the loop touched {len(touched)} of 20"
        assert len(set(touched)) == 20, "the loop repeated items instead of covering them"
        counts.append(turn.requests)

    assert counts == [2, 2, 2], (
        f"upstream chat completions per turn were {counts}; the criterion is that the "
        "number is identical across runs, which model-driven fan-out is not"
    )


def test_the_scratchpad_does_not_ride_home_in_the_checkpoint() -> None:
    """`mode="turn"`, and nothing else in this file would notice if it changed.

    The upstream default is `mode="thread"`: `after_agent` serialises the whole
    QuickJS heap and writes it into graph state so variables survive to the next
    turn. Aleph checkpoints graph state to Postgres per conversation, and the
    payload is capped at `memory_limit` — up to 32 MB a turn, per thread, for a
    scratchpad whose only job is one fan-out that finishes inside the turn that
    started it.

    This asserts configuration rather than behaviour, and that is a deliberate,
    stated compromise: `_quickjs_snapshot_payload` is a `PrivateStateAttr`,
    stripped from the graph's output, and — measured — a variable set in turn
    one is `undefined` in turn two under BOTH modes even with a checkpointer
    attached. There is no observable difference to assert from outside, so the
    choice is between reading the flag that decides whether bytes are produced
    at all and having no check. A vacuous test that reads the state key would
    have passed under either mode; this one goes red when somebody switches it.
    """
    _quickjs()

    async def look_up(query: str, top_k: int = 6) -> str:
        """Scan the wiki for one query."""
        return "ok"

    interpreter = build_interpreter_middleware(tools=_toolset(search_wiki=look_up))[0]
    assert interpreter._snapshot_between_turns is False, (
        "the interpreter is snapshotting its heap between turns; that payload "
        "goes into the conversation checkpoint in Postgres"
    )
    assert interpreter._mode == "turn"


@pytest.mark.asyncio
async def test_the_scratchpad_keeps_project_scope() -> None:
    """Measured defect: a tool exposed to PTC by name loses its RunnableConfig.

    `ptc=["search_wiki"]` — the obvious implementation, and the one the upstream
    docs show — hands the live tool to `BaseTool.arun` from the REPL's worker
    thread with a fresh empty config. Every Aleph orchestrator tool reads its
    project id out of that config, so every item in the loop answers "no project
    scope on this run" while the turn reports success. Measured: `thread_id=None`
    by name, the real id through `guarded_ptc_tools`. The mutation that turns
    this red is `ptc=list(PTC_ALLOWLIST)` in `build_interpreter_middleware`.
    """
    _quickjs()
    seen: list[str | None] = []

    async def look_up(query: str, config: RunnableConfig, top_k: int = 6) -> str:
        """Scan the wiki for one query, recording the config it was handed."""
        configurable = (config or {}).get("configurable") or {}
        seen.append(configurable.get("thread_id"))
        return "ok"

    fixture = _toolset(search_wiki=look_up)
    turn = await _run_script('await tools.searchWiki({query: "x"});', fixture)

    assert seen == [_THREAD_ID], (
        f"the tool saw thread_id={seen}; without it `_project_id_from_config` "
        "returns None and every wiki call in the loop answers 'unavailable'"
    )
    assert "ok" in turn.text


@pytest.mark.asyncio
async def test_one_bad_item_does_not_lose_the_batch() -> None:
    """Measured defect: an unguarded raise discards the items already done.

    PTC bypasses `ToolNode` and therefore `AlephAgentMiddleware.awrap_tool_call`
    (`WS-E1b`), so without the wrapper the tool's exception propagates out of
    `eval` and out of `ainvoke` — item 20 failing turns 19 completed items into
    zero coverage, which is the exact number this workstream is measured on.
    """
    _quickjs()

    async def look_up(query: str, top_k: int = 6) -> str:
        """Scan the wiki for one query; one item is unreachable."""
        if query == "item-7":
            raise RuntimeError("this source is unreachable")
        return f"{query}: ok"

    fixture = _toolset(search_wiki=look_up)
    code = """
const out = [];
for (let i = 0; i < 20; i++) { out.push(await tools.searchWiki({query: `item-${i}`})); }
const bad = out.filter(r => String(r).indexOf("tool error") !== -1).length;
`covered ${out.length - bad}/${out.length}`;
"""
    turn = await _run_script(code, fixture)
    assert "covered 19/20" in turn.text, turn.text
    assert turn.requests == 2

    # The same escape leaks a JavaScript engine. An exception that leaves the
    # graph skips the middleware's `after_agent` hook, which is the only thing
    # that evicts the REPL slot, so its non-daemon `quickjs-worker-*` thread and
    # its QuickJS runtime stay alive — measured at 0 → 1 → 2 over successive
    # unguarded failures. In an API process that lives for weeks that is a
    # thread per failed turn; it is also why the mutant that removes the guard
    # hangs this suite at exit instead of failing it.
    assert [th.name for th in threading.enumerate() if "quickjs" in th.name] == []


@pytest.mark.asyncio
async def test_interpreter_cannot_reach_the_host() -> None:
    """The sandbox is the reason this is allowed near the API process at all.

    QuickJS has no host bindings of its own; everything the script can reach
    Aleph gave it. So the assertion is in two halves: nothing that could touch
    the filesystem, the network or the process exists, and the `tools` namespace
    contains exactly the allowlist and nothing else — no `task` (subagent
    dispatch, which would put unbounded fan-out back), and no mutating tool.
    """
    _quickjs()

    async def look_up(query: str, top_k: int = 6) -> str:
        """Scan the wiki for one query."""
        return "ok"

    fixture = _toolset(search_wiki=look_up)

    escapes = (
        "require",
        "fetch",
        "process",
        "XMLHttpRequest",
        "WebSocket",
        "importScripts",
        "Deno",
        "readFile",
        "open",
    )
    probe = (
        "JSON.stringify(["
        + ",".join(
            f'typeof globalThis.{name} !== "undefined" ? "{name}" : null' for name in escapes
        )
        + "].filter(Boolean));"
    )
    reachable = (await _run_script(probe, fixture)).text
    assert "[]" in reachable, f"the sandbox exposes host capabilities: {reachable}"

    namespace = (await _run_script("JSON.stringify(Object.keys(tools).sort());", fixture)).text
    expected = json.dumps(sorted(_camel(name) for name in PTC_ALLOWLIST)).replace(" ", "")
    assert expected in namespace.replace(" ", ""), f"{namespace} != {expected}"
    for withheld in PTC_WITHHELD:
        assert _camel(withheld) not in namespace, f"{withheld} is reachable from a loop"

    # `task` is installed as a top-level function, not under `tools`, so it has
    # to be probed for by name. `subagents=True` (the upstream default) puts it
    # there: subagent dispatch from inside the loop is a model loop per item,
    # which is the unbounded fan-out this workstream exists to remove, dispatched
    # with no parent approval because the one `eval` was approved.
    dispatch = (await _run_script("typeof globalThis.task;", fixture)).text
    assert "undefined" in dispatch, f"task() is callable from inside the REPL: {dispatch}"

    escaped_eval = (await _run_script('typeof globalThis["eval_tool"];', fixture)).text
    assert "undefined" in escaped_eval


@pytest.mark.asyncio
async def test_the_loop_has_a_ceiling() -> None:
    """A runaway script is a bounded, recoverable error, not an occupied worker.

    `INTERPRETER_MAX_PTC_CALLS` is the bound on Aleph's own read load — the
    gateway still sees one completion either way. The budget error is surfaced
    to the model deliberately, so it shortens its script rather than the turn
    dying.
    """
    _quickjs()
    calls: list[str] = []

    async def look_up(query: str, top_k: int = 6) -> str:
        """Scan the wiki for one query."""
        calls.append(query)
        return "ok"

    fixture = _toolset(search_wiki=look_up)
    over = INTERPRETER_MAX_PTC_CALLS + 5
    code = f"""
for (let i = 0; i < {over}; i++) {{ await tools.searchWiki({{query: `q-${{i}}`}}); }}
"done";
"""
    turn = await _run_script(code, fixture)
    assert "PTCCallBudgetExceeded" in turn.text, turn.text
    assert len(calls) <= INTERPRETER_MAX_PTC_CALLS, len(calls)
    assert turn.requests == 2
