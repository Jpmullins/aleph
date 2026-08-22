"""The agent grades its own work before handing it over (`WS-H3`).

`RubricMiddleware` ships in deepagents 0.6.6 — the version pinned in
`apps/api/pyproject.toml` — and it does **nothing at all** unless something puts
a `rubric` onto the run's state. Nothing does. The graph is compiled once at
start-up and the Node bridge constructs `new HttpAgent({ url: AGENT_URL })` with
no state channel, so there is no path from a browser to that key. This module is
the server-side path: read the standard the project wrote down, put it on state
before the grader looks for it, and pay for the grader's calls out of the same
ledger as everything else.

**Where the standard lives, and why there.** `/memories/rubric.md`, read through
the agent's own `CompositeBackend` — the same route, the same per-project
`StoreBackend`, the same durable Postgres-backed langgraph store the agent's
memories already use. Deliberately not a new table and not a settings field:

* it is already writable by exactly the people and the agent that should write
  it, through the `write_file` tool that already exists, so this ships a reader
  for a producer that is already wired rather than a column nothing fills;
* it is already scoped per project (`(<project_uuid>, "memories")`), so one
  project's standard cannot grade another's answer;
* reading it *through the backend factory* rather than through a namespace this
  module reimplements means there is one definition of where `/memories/` lives.
  A second copy of that convention would go stale silently, and the failure —
  grading quietly switched off — is invisible: a turn with no rubric and a turn
  whose rubric could not be found look identical from outside.

**Ordering is the whole thing, and it is not a style preference.**
`ProjectRubricMiddleware` must run *before* `RubricMiddleware`'s own
`abefore_agent`. Hooks run in list order, so this module returns the pair as one
ordered list rather than asking the call site to get it right. Reversed, grading
does not stop — it silently corrupts:

* `_reset_for_new_rubric` sees no rubric and returns early, so `_active_rubric`
  and `_current_grading_run_id` are never set;
* `_prepare_evaluation` then mints a *fresh* `uuid4` per iteration, so the
  evaluations of one grading run no longer share an id;
* and `_rubric_iterations` is never reset between turns. It only ever
  increments, so after `max_iterations` grader calls **on the thread**, every
  later turn hits `max_iterations_reached` on its first evaluation and the
  revision loop is dead for the rest of the conversation.

Measured, not reasoned about: `test_the_grader_never_sees_a_rubric_when_the_source_runs_late`.

**What this costs, counted.** Measured against a live stack with
`scripts/_acceptance/agent_turn_probe.py`, an ungraded no-tool turn is **1**
upstream chat completion and an ungraded single-tool turn is **2**. Measured
in-process at `max_iterations=2`, the same no-tool turn graded is **2** when the
first verdict is `satisfied` and **4** when it is not — a revision is a whole
extra agent turn, tools included, plus a second grader call. So the honest
statement is: grading at least doubles a turn, and its worst case is four times
one. Backlog E5 already reports unexplained gateway rate limiting attributed to
fan-out, which is why the default here is 2 and not the library's 3 (which would
make the worst case six). The numbers are asserted in
`test_grading_costs_this_many_upstream_requests`, not estimated.

The three parts are returned as one list and it is three, not two:
`ProjectRubricMiddleware`, then `CostedRubricMiddleware` (the library's grader
with the turn's cost identity published around it), then `FrontendHandoffGuard`.
`before_*` hooks run in list order and `after_*` hooks run in reverse, so the
first and the last are each first in the direction that matters.

**A rubric is untrusted text.** It arrives from a file the agent can write, and
an ingested document can in principle talk the agent into writing one. It cannot
grant capability — it only describes "done" — but it can spend money by being
unsatisfiable, which is what `max_iterations` bounds, and it can be enormous,
which is what `MAX_RUBRIC_CHARS` bounds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog
from deepagents.middleware.rubric import RubricMiddleware, RubricState
from langchain.agents.middleware.types import AgentMiddleware, hook_config

if TYPE_CHECKING:
    from collections.abc import Callable

    from deepagents.middleware.rubric import RubricEvaluation
    from langchain_core.language_models import BaseChatModel

    from aleph_api.settings import Settings

_log = structlog.get_logger(__name__)

#: Where a project writes down what a finished answer looks like.
#:
#: An agent-facing path, not a store key: it is resolved through the same
#: `CompositeBackend` the filesystem tools write through, so `/memories/` is
#: routed to the per-project `StoreBackend` by the one definition that already
#: exists in `copilot_agent`.
RUBRIC_PATH = "/memories/rubric.md"

#: Cost tag on every grader call. `select ... where purpose = 'assistant.rubric.grader'`
#: is how "what did self-grading cost this project" is answered, so it has to be
#: distinct from `assistant.turn` — the grader is a different decision to fund.
RUBRIC_GRADER_PURPOSE = "assistant.rubric.grader"

#: Grader evaluations per turn. The library's default is 3; this is 2.
#:
#: Not timidity: iteration N+1 is a *whole extra agent turn*, tools included, and
#: the failure mode of a rubric the grader can never satisfy is a user message
#: that silently becomes `max_iterations + 1` turns. Two is the smallest value
#: that still allows the loop to do its job — grade, revise once, grade again.
DEFAULT_MAX_ITERATIONS = 2

#: Upper bound on the rubric text handed to the grader.
#:
#: The rubric is interpolated into every grader prompt, so its length is
#: multiplied by the iteration count on every graded turn. Truncation is loud
#: (a warning naming the length) because a standard that was silently cut in
#: half would grade against criteria nobody can see.
MAX_RUBRIC_CHARS = 4_000


async def read_rubric(backend: Any, path: str = RUBRIC_PATH) -> str | None:
    """The project's rubric, or `None` when it has not written one.

    `None` and `""` mean the same thing to the caller — the middleware is inert —
    but they are reached differently and both are normal. A project that has
    never written a rubric has no file; a project that emptied the file has one
    with nothing in it. Neither is an error and neither is logged as one.

    A backend read that *fails* is different and is logged: that is the case
    where a standard exists and is not being applied, which otherwise looks
    exactly like the two normal cases from outside.
    """
    if backend is None:
        return None
    try:
        result = await backend.aread(path)
    except Exception:
        _log.exception("rubric.read_failed", path=path)
        return None

    error = getattr(result, "error", None)
    if error:
        # A missing file is the ordinary case, not a fault: most projects have
        # not written a rubric and grading is opt-in. Debug, not warning —
        # a warning on every ungraded turn is a warning people learn to ignore.
        _log.debug("rubric.absent", path=path, detail=str(error)[:200])
        return None

    file_data = cast("dict[str, object] | None", getattr(result, "file_data", None))
    content = file_data.get("content") if isinstance(file_data, dict) else None
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text:
        return None
    if len(text) > MAX_RUBRIC_CHARS:
        _log.warning(
            "rubric.truncated",
            path=path,
            length=len(text),
            limit=MAX_RUBRIC_CHARS,
            detail="the rubric is graded against every iteration; the tail was dropped",
        )
        text = text[:MAX_RUBRIC_CHARS]
    return text


def log_evaluation(evaluation: RubricEvaluation) -> None:
    """One structured line per grader verdict.

    `RubricMiddleware` deliberately does **not** mutate the response when
    grading ends in anything but `satisfied` (rubric.py:305-318), so a run that
    gave up looks, from the messages alone, exactly like a run that succeeded.
    This is the only place a non-satisfied termination is visible without
    reading `_rubric_status` off a checkpointed thread.

    Exceptions raised here are swallowed by the library and logged at error
    level; this must never be used for control flow.
    """
    failing = [c.get("name", "?") for c in evaluation.get("criteria", []) if not c.get("passed")]
    _log.info(
        "rubric.evaluated",
        grading_run_id=evaluation.get("grading_run_id"),
        iteration=evaluation.get("iteration"),
        result=evaluation.get("result"),
        failing=failing,
        explanation=str(evaluation.get("explanation", ""))[:500],
    )


class ProjectRubricMiddleware(AgentMiddleware[RubricState, Any, Any]):
    """Puts the project's rubric on state, so `RubricMiddleware` has one to use.

    Declares `RubricState` as its own `state_schema` on purpose. `rubric` is a
    channel `RubricMiddleware` contributes, and a LangGraph write to a channel
    no schema declares is **discarded in silence** — the node returns the key,
    the update vanishes, and the grader never runs while every step reports
    success (rule #7; four shipped defects had exactly this shape). Declaring it
    here means this middleware is correct on its own rather than correct only
    while it happens to be listed next to the one that owns the key.
    """

    state_schema = RubricState

    def __init__(
        self,
        *,
        backend_factory: Callable[[Any], Any] | None = None,
        path: str = RUBRIC_PATH,
    ) -> None:
        # The SAME factory `create_deep_agent` was given, never a captured
        # instance — the identical argument `AuthoredSkillsMiddleware` makes.
        # deepagents resolves a backend by calling the factory with a runtime,
        # so calling it here reaches the store the filesystem tools just wrote
        # through; a captured instance would silently read a different store the
        # moment the backend becomes per-request.
        self._backend_factory = backend_factory
        self._path = path
        super().__init__()

    async def abefore_agent(self, state: RubricState, runtime: Any) -> dict[str, Any] | None:
        """Resolve this turn's rubric. Returns `None` — inert — when there is none.

        The file is re-read every turn, and it wins over whatever is already on
        state. Both halves of that matter:

        * *Re-read*, because a rubric is a standard someone edits. `rubric` lives
          on a checkpointed thread, so reading it once and keeping it would mean
          an analyst rewriting `/memories/rubric.md` mid-conversation sees no
          change and no explanation. `RubricMiddleware` handles the swap
          correctly — a different rubric string starts a fresh grading run with a
          fresh iteration budget, which is exactly right for a new standard.
        * *Only when the file has something*, because the alternative is that a
          store outage silently switches grading off. `None` leaves the rubric
          already on state alone, so a failed read degrades to "grade against
          what we had" rather than to "stop grading and say nothing".
        """
        backend = self._resolve_backend(runtime)
        rubric = await read_rubric(backend, self._path)
        if not rubric:
            return None
        if rubric == state.get("rubric"):
            # Returning the identical string would still be a channel write, and
            # `_reset_for_new_rubric` reads `_active_rubric` rather than the write
            # — so this is only to keep the update log honest about what changed.
            return None
        return {"rubric": rubric}

    def _resolve_backend(self, runtime: Any) -> Any:
        if self._backend_factory is None:
            return None
        try:
            return self._backend_factory(runtime)
        except Exception:
            _log.exception("rubric.backend_unresolved")
            return None


class CostedRubricMiddleware(RubricMiddleware):
    """`RubricMiddleware`, with the grader's spend joined to the turn that caused it.

    The grader's model *is* cost-attributed — `grader_model` attaches an
    `AgentCostCallbackHandler` — but a `ModelCall` row is only half a record if
    it cannot be joined to the turn. `scripts/_acceptance/agent_turn_probe.py`
    counts a turn's upstream requests with
    `select count(*) from model_calls where agent_run_id = :rid`, so a grader row
    with a NULL run id is spend the probe cannot see: the number it prints would
    say self-grading is free, which is the single most misleading thing it could
    say about this feature.

    And NULL is what it was. Measured: `test_the_grader_rows_join_to_the_turn`
    failed with `{None}` before this class existed. The run id travels in
    `config["configurable"]`, and LangChain does not merge `configurable` into
    the callback `metadata` the cost handler reads — that is the same channel
    defect that kept `model_calls.agent_run_id` unconditionally NULL for the
    whole life of the column. `AlephAgentMiddleware.awrap_model_call` bridges it
    with a task-local scope, but the grader is a *separate* agent invoked from
    `aafter_agent`, so it never passes through that wrapper.

    So publish the scope around the grading call. The context variable is copied
    into any task the grader's own graph spawns, and it is reset the moment
    grading returns, so no later call can inherit this turn's identity.
    """

    def _scope(self, runtime: Any) -> Any:
        from aleph_api.chat_runs import ModelCallScope, model_call_scope, run_id_from_runtime

        return model_call_scope(
            ModelCallScope(
                agent_run_id=run_id_from_runtime(runtime),
                # The model the grader actually names, not the one resolved when
                # the graph was built: a project that switches profile mid-session
                # would otherwise have every later row mislabelled.
                model=getattr(self._model, "model_name", None),
            )
        )

    # `@hook_config` does not survive an override, and its absence does not
    # raise. It is what builds the conditional edge back to the model
    # (factory.py:1765-1775), so without it the hook still returns
    # `jump_to='model'` and the graph goes to END anyway: one grade, no
    # revision, no error, `_rubric_status` left sitting at `needs_revision`.
    # Measured — dropping it from BOTH methods takes the graded turn from four
    # upstream requests to two and reds five tests. It is on both because
    # `_get_can_jump_to` (factory.py:491-523) reads the sync method first and
    # falls back to the async one, so either alone would work today and neither
    # alone is a thing to depend on.
    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: RubricState, runtime: Any) -> dict[str, Any] | None:
        """Sync variant. Overridden too, or a sync caller silently loses attribution."""
        with self._scope(runtime):
            return super().after_agent(state, runtime)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: RubricState, runtime: Any) -> dict[str, Any] | None:
        with self._scope(runtime):
            return await super().aafter_agent(state, runtime)


def _pending_frontend_calls(state: RubricState) -> list[str]:
    """Names of tool calls this turn is handing to the browser, if any.

    TWO signals, because which one is available depends on where the grading
    middleware sits relative to `CopilotKitMiddleware`, and a guard that only
    works in one arrangement is a guard that will be silently disarmed by a
    reordering nobody thought was risky.

    * Before CopilotKit's own `after_agent` has run, the calls are parked in
      `copilotkit.intercepted_tool_calls` and the last AI message is clean.
    * After it has run, that key is cleared and the calls are back on the last
      AI message (copilotkit_lg_middleware.py:649-682).

    The second signal is worth having on its own merits: an AI message carrying
    tool calls with no answering `ToolMessage` is not a finished turn under any
    reading, and appending a revision prompt after one produces a message
    sequence most providers reject outright.
    """
    raw_state = cast("dict[str, object]", state)
    copilotkit: object = raw_state.get("copilotkit")
    intercepted: object = (
        cast("dict[str, object]", copilotkit).get("intercepted_tool_calls")
        if isinstance(copilotkit, dict)
        else None
    )
    if isinstance(intercepted, list) and intercepted:
        return [
            str(cast("dict[str, object]", c).get("name"))
            for c in cast("list[object]", intercepted)
            if isinstance(c, dict)
        ]

    messages = cast("list[object]", raw_state.get("messages") or [])
    if not messages:
        return []
    calls = getattr(messages[-1], "tool_calls", None)
    if not isinstance(calls, list) or not calls:
        return []
    return [
        str(cast("dict[str, object]", c).get("name"))
        for c in cast("list[object]", calls)
        if isinstance(c, dict)
    ]


class FrontendHandoffGuard(AgentMiddleware[RubricState, Any, Any]):
    """Do not grade a turn whose whole point is to hand a tool call to the browser.

    `open_page`, `focus_tab` and `highlight_claim` are CopilotKit *frontend*
    tools: they run in the browser, not here. `CopilotKitMiddleware.after_model`
    strips those calls off the AI message and parks them in
    `state["copilotkit"]["intercepted_tool_calls"]`
    (copilotkit_lg_middleware.py:587-637), which leaves the message with no tool
    calls at all — so the agent looks *finished* to every `after_agent` hook,
    including the grader's.

    That is the whole problem. The turn is not finished; it is mid-handoff. A
    grader looking at that transcript sees an answer that did nothing, votes
    `needs_revision`, and `jump_to='model'` resumes the agent — so instead of
    the browser being asked to open the page, the model writes another paragraph
    about it. Nothing errors, and the pane simply never opens.

    So: blank the rubric for this evaluation only. `_prepare_evaluation` no-ops
    on a falsy rubric, and `ProjectRubricMiddleware` re-reads the file on the
    next turn, so grading resumes by itself once the handoff completes.

    Listed LAST in the returned middleware list, which is what makes it run
    FIRST here: `before_*` hooks run in list order and `after_*` hooks run in
    reverse, so last-in-the-list is first-to-grade.
    """

    state_schema = RubricState

    async def aafter_agent(self, state: RubricState, runtime: Any) -> dict[str, Any] | None:
        if not state.get("rubric"):
            return None
        pending = _pending_frontend_calls(state)
        if not pending:
            return None
        _log.info(
            "rubric.skipped_frontend_handoff",
            pending=pending,
            detail="the turn ends in a browser tool call; grading it would swallow the handoff",
        )
        return {"rubric": ""}


def grader_model(settings: Settings) -> BaseChatModel:
    """The grader's own gateway-pointed, cost-attributed model.

    Built by `_gateway_chat_model` like every other model on the agent path, so
    it inherits the gateway `base_url`, the shared concurrency-limited HTTP
    client, `stream_usage=True` and — the point — its own
    `AgentCostCallbackHandler`. Without this the grader would be the one model
    in the process calling the gateway with nobody counting, which is precisely
    the hole `WS-MEP-1` exists to close.

    `Capability.JUDGE`, the same capability the `reviewer` subagent resolves:
    grading a transcript against criteria is the judging job, and a project that
    has bound a cheap model to JUDGE means it for this too.
    """
    from aleph_api.copilot_agent import (
        _gateway_chat_model,  # pyright: ignore[reportPrivateUsage] — the ONE constructor every agent model goes through (rule #12); module-private to the api
    )
    from aleph_core.schemas.model_profile import Capability

    return _gateway_chat_model(settings, purpose=RUBRIC_GRADER_PURPOSE, capability=Capability.JUDGE)


def build_grading_middleware(
    *,
    settings: Settings,
    backend_factory: Callable[[Any], Any] | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    model: BaseChatModel | None = None,
) -> list[AgentMiddleware[Any, Any, Any]]:
    """The grading step, as one correctly ordered list.

    Returned as a list rather than as two exports so the call site cannot get
    the order wrong: splat it into `middleware=[...]` and the source is
    guaranteed to run before the grader. See this module's docstring for what
    the reversed order does — it does not fail, it degrades into a grading loop
    whose budget never resets.

    `model` is an injection seam for tests, which script a grader's verdicts;
    production passes nothing and gets `grader_model(settings)`.

    Raises:
        ValueError: from the library, when `max_iterations` is outside [1, 20].
    """
    return [
        ProjectRubricMiddleware(backend_factory=backend_factory),
        CostedRubricMiddleware(
            model=model if model is not None else grader_model(settings),
            max_iterations=max_iterations,
            on_evaluation=log_evaluation,
        ),
        # Last on purpose — see the class docstring. `after_*` hooks run in
        # reverse list order, so this one grades-or-vetoes before the grader.
        FrontendHandoffGuard(),
    ]
