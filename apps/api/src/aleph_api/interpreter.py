"""The assistant's scratchpad: one bounded loop instead of a guess at how many
helpers to launch.

**The defect this removes.** When the analyst asks for the same thing to be done
to twenty items, the orchestrator decides — one turn at a time, by sampling from
a distribution — how many helpers to dispatch and which items to give them. Two
things go wrong at once and neither reports itself. It *samples*: three papers
get checked and seventeen do not, and the answer reads as though all twenty were.
And the number of upstream chat completions the turn issues is whatever the model
felt like, which is the most likely cause of the rate limiting the owner has been
hitting.

`WS-E1c`'s retry and `MEP-2`'s shared limiter manage the *symptom*: once the
requests exist, they are spaced out and capped. This module removes the *cause*.
A fan-out written as JavaScript inside one `eval` call issues **one** upstream
chat completion no matter how many items the loop touches — measured below at
20/20 items for 2 completions (the eval turn and the answer turn), and the 2 does
not move when the 20 becomes 200.

**What it is.** `langchain-quickjs`'s `CodeInterpreterMiddleware` gives the agent a
QuickJS REPL with no filesystem, no network, no `require`, no real clock — a
scratchpad for pure computation — plus a `tools.*` namespace ("programmatic tool
calling", PTC) through which the loop reaches back into Aleph's own read-only
tools.

**Two things the upstream default gets wrong for Aleph, both measured, both fixed
here rather than worked around at the call site.**

1. *A tool exposed to PTC by name loses its RunnableConfig.* The bridge runs
   the live tool through `BaseTool.arun` from the REPL's own worker thread, and
   the config that reaches the tool's `config: RunnableConfig` parameter is a
   fresh empty one. Every Aleph orchestrator tool resolves its project scope
   from that config (`_project_id_from_config`), so a plainly-exposed
   `search_wiki` answers *"Wiki search is unavailable (no project scope on this
   run)"* — for every item, silently, while the turn reports success. Measured
   in this repository's venv: `ptc=["search_wiki"]` saw `thread_id=None`; the
   same tool exposed as the wrapper below saw the real thread id, because what
   `arun` runs is then the wrapper, and the wrapper's own `inner.ainvoke(...)`
   resolves against the live graph context. The wrapper *also* forwards the
   injected `ToolRuntime`'s config explicitly — belt and braces, since a
   contextvar surviving a hop between two event loops is not a property to
   leave to a minor release of langchain-core.

2. *A raising tool takes the whole batch with it, and leaks a JavaScript engine
   doing it.* PTC bypasses `ToolNode`, so it also bypasses
   `AlephAgentMiddleware.awrap_tool_call` (`WS-E1b`), the guard that turns a
   throwing tool into a `ToolMessage` the model can route around. Measured: an
   unguarded raising tool propagated its `RuntimeError` straight out of
   `agent.ainvoke` — item 20 failing loses items 1-19, which is 0/20 coverage on
   the one number this workstream exists to move. Worse, because the exception
   escapes the graph, the middleware's `after_agent` hook never runs and never
   evicts the REPL slot: each escape left one live non-daemon `quickjs-worker-*`
   OS thread behind (measured 0 → 1 → 2 over successive failures), and those
   threads are why the pytest process running that mutant would not exit. In an
   API process that lives for weeks, that is a thread and a QuickJS runtime per
   failed turn. The wrapper catches per item and returns an error string, so
   nineteen items still land and the slot is still evicted.

**Read-only, deliberately.** PTC also bypasses `interrupt_on` / human-in-the-loop
approval — one approved `eval` can drive a hundred tool calls with no further
gate. So the allowlist below is read-only tools only, and `PTC_WITHHELD` records
why each of the others is not there. `subagents=False` for the same reason and
one more: `task()` from inside the REPL dispatches a subagent, and a subagent is
its own model loop — allowing it would put the unbounded, non-deterministic
fan-out back inside the mechanism built to remove it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Sequence

_log = structlog.get_logger(__name__)

__all__ = [
    "INTERPRETER_MAX_PTC_CALLS",
    "INTERPRETER_MEMORY_LIMIT_BYTES",
    "INTERPRETER_TIMEOUT_S",
    "INTERPRETER_TOOL_NAME",
    "PTC_ALLOWLIST",
    "PTC_WITHHELD",
    "FanOutPolicyMiddleware",
    "build_interpreter_middleware",
    "guarded_ptc_tools",
]

#: The name the model sees. `eval` is the upstream default; pinned here so the
#: acceptance probe and the registration test can name one constant rather than
#: a string literal that drifts.
INTERPRETER_TOOL_NAME: Final = "eval"

#: Wall clock for one `eval`, in seconds. This is the only thing standing
#: between a runaway script and the API process: the agent runs in-band with the
#: HTTP request, so an eval that never returns is an occupied worker, not a
#: degraded feature. Generous enough for a loop of ~100 database reads,
#: nowhere near an unbounded wait.
INTERPRETER_TIMEOUT_S: Final = 60.0

#: QuickJS heap ceiling, in bytes. Half the upstream default: the scratchpad
#: holds identifiers and short strings between tool calls, not corpora, and
#: `INTERPRETER_MAX_RESULT_CHARS` already truncates what comes back.
INTERPRETER_MEMORY_LIMIT_BYTES: Final = 32 * 1024 * 1024

#: How many `tools.*` calls one `eval` may make. This bounds Aleph's OWN read
#: load, not the gateway's: every allowlisted tool is a local database read, and
#: the gateway sees exactly one completion per eval regardless. A sweep over a
#: few hundred wiki pages fits; an accidental `while (true)` does not.
INTERPRETER_MAX_PTC_CALLS: Final = 128

#: Truncation applied independently to the result and to captured console
#: output before either reaches the conversation. The entire point of the
#: scratchpad is that the twenty intermediate results stay OUT of the model's
#: context; a generous cap here would hand them straight back.
INTERPRETER_MAX_RESULT_CHARS: Final = 4_000

#: Orchestrator tools exposed inside the REPL, by name. Read-only only — see
#: the module docstring on why HITL cannot reach a PTC call.
PTC_ALLOWLIST: Final[tuple[str, ...]] = (
    "search_wiki",
    "wiki_curation_status",
    "wiki_schema",
    "wiki_lint_report",
    "list_connectors",
    "diagnose_platform",
    # WS-A2. Both are pure reads over the kernel's declaration graph and change
    # nothing — `preview_removal` in particular is the one an agent should be
    # able to call freely, since the whole design is that a refusal must be
    # predictable before it is attempted.
    "list_capabilities",
    "preview_removal",
    "plugin_health",
    # WS-H6. Reading a ticket's progress is a pure read of rows the job wrote,
    # and it is the one of the three a loop genuinely wants: "poll until this
    # finishes" is the natural shape, and it costs a SELECT.
    "check_background_task",
)

#: The orchestrator tools deliberately NOT exposed, and why. This is half of a
#: two-way partition: `test_interpreter_middleware.py` asserts every tool the
#: orchestrator carries appears in exactly one of these two, so adding a tool
#: without deciding whether a loop may call it fails a test rather than quietly
#: defaulting either way.
PTC_WITHHELD: Final[dict[str, str]] = {
    "set_connector_enabled": (
        "mutates project configuration; PTC bypasses interrupt_on, so one "
        "approved eval could flip every connector with no further gate"
    ),
    "set_model_profile": "mutates project configuration; same bypass",
    "pin_to_brief": "writes a Briefs card; a loop would paper the tab",
    "compose_dossier": "writes a Briefs card; same",
    "spotlight": "mutates Briefs ordering through the audited action router",
    # WS-A2. These are the two that change what the system CAN DO, which is a
    # larger thing than any other tool here changes.
    "author_plugin": (
        "installs code this process will execute and an instruction the model "
        "will follow. PTC bypasses interrupt_on, so a loop could author "
        "plugins with no gate between the model and the kernel — the one place "
        "that must stay a deliberate act"
    ),
    "disable_plugin": (
        "removes capability other things may be standing on. The blast-radius "
        "refusal still holds, but `force=True` is reachable and a loop that can "
        "force is a loop that can dismantle the system it is running on"
    ),
    # WS-H6. Both cross the boundary from reading to spending.
    "start_background_task": (
        "each call fans out up to MAX_UNITS_PER_TASK jobs onto the bus, every "
        "one of which costs gateway calls. PTC bypasses interrupt_on, so a loop "
        "that starts tasks is a loop that bills the project with no gate"
    ),
    "cancel_background_task": (
        "stops work somebody else may be waiting on, and the ticket id comes "
        "from the model. A loop that can cancel by id can cancel a sweep it did "
        "not start"
    ),
}

#: Appended to the system prompt on every model call, after the REPL's own
#: description. The upstream prompt says the REPL exists; it does not say that
#: covering every item beats sampling a few, which is the behaviour this
#: workstream is about.
_FANOUT_POLICY_PROMPT = """\
### Covering a list

When the analyst asks for the same thing across MANY items — every page in a
list, every candidate paper, every finding in a lint report — do not dispatch
one helper per item and do not work through a sample. Write ONE `eval` that
loops over the whole list and calls `tools.*` per item, collect the results in
a variable, and return only the summary you actually need. Say how many items
you covered out of how many there were; if a `tools.*` call returned an
`[interpreter] tool error`, count it as not covered and say so rather than
leaving it out of the total.\
"""

#: Prefix on the string a failed `tools.*` call returns. The model is told about
#: it in the prompt above, so an item that failed is reported as uncovered
#: instead of silently dropping out of the denominator.
_TOOL_ERROR_PREFIX = "[interpreter] tool error"


def _guarded_ptc_tool(inner: BaseTool) -> BaseTool:
    """Wrap one tool for exposure inside the REPL.

    Two jobs, both of them the difference between the feature working and the
    feature looking like it works — see the module docstring, items 1 and 2.

    Being a wrapper at all is what restores project scope — exposing the live
    tool by name hands it to `BaseTool.arun` with a fresh empty config, and
    every Aleph tool then reports itself unavailable. `runtime: ToolRuntime` is
    declared so the config can also be forwarded explicitly: langgraph's
    injection detection reads the coroutine's signature, the PTC bridge
    replicates that detection and hands over a runtime derived from the `eval`
    call's own, so `runtime.config` is the live graph config with Aleph's
    `proj:<uuid>:<thread>` thread id in it.
    """
    if inner.args_schema is None:  # pragma: no cover - every @tool defines one
        msg = f"tool {inner.name!r} has no args_schema and cannot be exposed to the REPL"
        raise ValueError(msg)

    async def _guarded(runtime: ToolRuntime[Any, Any], **kwargs: Any) -> Any:
        try:
            return await inner.ainvoke(kwargs, config=runtime.config)
        except Exception as exc:
            # Deliberately swallowed and returned as a value. Raising here
            # propagates out of `eval` and out of `ainvoke` — measured — which
            # discards every item the loop had already completed.
            _log.warning(
                "interpreter.ptc_tool_failed",
                tool=inner.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return f"{_TOOL_ERROR_PREFIX} ({inner.name}): {type(exc).__name__}: {exc}"

    return StructuredTool(
        name=inner.name,
        description=inner.description,
        args_schema=inner.args_schema,
        coroutine=_guarded,
        metadata={"aleph_ptc_guarded": True},
    )


def guarded_ptc_tools(tools: Sequence[BaseTool]) -> list[BaseTool]:
    """The subset of `tools` the REPL may call, each wrapped by `_guarded_ptc_tool`.

    Raises when a name in `PTC_ALLOWLIST` matches nothing in `tools`. That is
    not defensive noise: `filter_tools_for_ptc` treats an unmatched name as
    "expose nothing", so renaming `search_wiki` without touching the allowlist
    would leave the scratchpad with one fewer tool, no error anywhere, and a
    model that quietly stops covering the list. Same failure shape as the wiki
    surface that shipped ten categories the client never declared.

    An orchestrator tool in neither list is withheld and logged rather than
    exposed by default; the partition itself is enforced by a test, because a
    new chat tool is not a reason for the API to refuse to boot.
    """
    by_name = {tool.name: tool for tool in tools}

    missing = [name for name in PTC_ALLOWLIST if name not in by_name]
    if missing:
        msg = (
            f"PTC_ALLOWLIST names {missing} which the orchestrator does not carry. "
            "Exposing an unmatched name is a silent no-op, so this is an error: "
            "fix the name in aleph_api.interpreter or drop it from the allowlist."
        )
        raise ValueError(msg)

    unclassified = sorted(set(by_name) - set(PTC_ALLOWLIST) - set(PTC_WITHHELD))
    if unclassified:
        _log.warning(
            "interpreter.unclassified_tools_withheld",
            tools=unclassified,
            detail=(
                "not reachable from the scratchpad. Add each to PTC_ALLOWLIST "
                "(read-only, safe to call in a loop with no HITL gate) or to "
                "PTC_WITHHELD with a reason."
            ),
        )

    return [_guarded_ptc_tool(by_name[name]) for name in PTC_ALLOWLIST]


class FanOutPolicyMiddleware(AgentMiddleware[Any, Any, Any]):
    """Appends `_FANOUT_POLICY_PROMPT` to the system message on every model call.

    Separate from the interpreter middleware rather than a subclass of it: the
    interpreter is upstream, `@beta`, and its prompt assembly lives behind a
    private method. Extending that method would make an Aleph behaviour depend
    on a private seam of a package whose API is documented as unstable.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(request.override(system_message=_extended(request.system_message)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(request.override(system_message=_extended(request.system_message)))


def _extended(system_message: SystemMessage | None) -> SystemMessage:
    if system_message is None:
        return SystemMessage(content=_FANOUT_POLICY_PROMPT)
    return SystemMessage(content=f"{system_message.text}\n\n{_FANOUT_POLICY_PROMPT}")


def build_interpreter_middleware(
    *, tools: Sequence[BaseTool]
) -> list[AgentMiddleware[Any, Any, Any]]:
    """The middleware to append to the orchestrator's list, in order.

    Append — never insert. Middleware earlier in the list is *outer*: it wraps
    the ones after it, so the last entry is the one that sees the request the
    model will actually receive. The interpreter needs exactly that, because
    `request.tools` is what it filters the PTC allowlist against; placed ahead
    of a middleware that adds or removes a tool it would install bridges for a
    toolset that no longer exists. It also has to stay INSIDE
    `AlephAgentMiddleware`, which is first and therefore outermost, so a
    throwing `eval` still becomes a `ToolMessage` rather than ending the turn.

    Raises `RuntimeError` when `langchain-quickjs` is not installed. Fail-closed
    on purpose: the alternative is an assistant that silently lacks the tool its
    own system prompt tells it to use.
    """
    try:
        from langchain_quickjs import (  # pyright: ignore[reportMissingImports]
            CodeInterpreterMiddleware,
        )
    except ImportError as exc:  # pragma: no cover - exercised by the pin, not by CI
        msg = (
            "The agent scratchpad needs langchain-quickjs, which is not installed. "
            "Pin `langchain-quickjs~=0.2.0` in apps/api/pyproject.toml (it requires "
            "deepagents>=0.6.8, inside the existing <0.7 pin) and re-run `uv sync "
            "--all-packages --all-extras`."
        )
        raise RuntimeError(msg) from exc

    # Annotated because `PTCOption` is `list[str | BaseTool]` and `list` is
    # invariant: without it the guarded tools read as the wrong type and the
    # only signal is a pyright warning nobody has to act on.
    exposed: list[str | BaseTool] = list(guarded_ptc_tools(tools))
    interpreter: AgentMiddleware[Any, Any, Any] = CodeInterpreterMiddleware(
        tool_name=INTERPRETER_TOOL_NAME,
        # One eval call = one gateway request, regardless of how many items the
        # loop touches. `subagents=True` would undo that: `task()` from inside
        # the REPL starts a subagent, which is a model loop of its own, dispatched
        # with no parent-level approval because the eval was approved once.
        subagents=False,
        ptc=exposed,
        # "turn", not the default "thread". `thread` serialises the whole QuickJS
        # heap into the checkpoint between turns — up to `memory_limit` bytes per
        # conversation, in Postgres. The scratchpad is for one fan-out; nothing
        # needs to survive the turn that produced it.
        mode="turn",
        timeout=INTERPRETER_TIMEOUT_S,
        memory_limit=INTERPRETER_MEMORY_LIMIT_BYTES,
        max_ptc_calls=INTERPRETER_MAX_PTC_CALLS,
        max_result_chars=INTERPRETER_MAX_RESULT_CHARS,
        # The loop's own progress notes. Truncated to the same ceiling as the
        # result, so a chatty script cannot smuggle the intermediate results the
        # scratchpad exists to keep out of context.
        capture_console=True,
    )
    return [interpreter, FanOutPolicyMiddleware()]
