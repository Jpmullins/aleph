"""Aleph's own agent middleware: a tool failure is a message, not a dead run.

The assistant has 27 tools. Before this, any one of them throwing — a permission
check, a database hiccup, a missing dictionary key, a 404 for a page that does
not exist — killed the whole conversation on the spot. That is not how agents
are supposed to work: a normal agent reads the error, says "that did not work,
let me try another way", and keeps going. Ours could not, because it never got
to see the error.

The cause is LangChain's default, and it is a deliberate one: `ToolNode` wired
with no `handle_tool_errors` re-raises anything that is not a schema-validation
error. That is correct for a library and wrong for this agent.

The defect is structural rather than local. Six of the eleven orchestrator tools
contain no `try:` at all, and *every* tool — guarded or not — calls
`_project_id_from_config` OUTSIDE its try block, which reaches
`require_project_access` and can raise `PermissionDenied`. So the tools that
looked defended were defended against the wrong thing.

**What this must not become.** Swallowing `PermissionDenied` and handing the
model a friendly sentence must not turn into a way for the agent to keep probing
a project it has no access to. Authorization failures are still refusals — the
model is told it may not, not told to try again — and they are logged
distinctly, so a run that spends its turn bouncing off a permission boundary is
visible rather than merely quiet.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any, Literal

import structlog
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from aleph_api.chat_runs import (
    TOOL_FAILED,
    TOOL_FINISHED,
    TOOL_STARTED,
    ModelCallScope,
    ToolEventClock,
    model_call_scope,
    record_tool_event,
    run_id_from_runtime,
    subagent_from_runtime,
)
from aleph_core.errors import AlephError, NotFound, PermissionDenied

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest
    from langgraph.types import Command

_log = structlog.get_logger(__name__)

#: Per-exception guidance, so the message the model reads suggests a next move
#: rather than only reporting a stop. A tool error the model cannot act on is a
#: slower way to end the turn.
_ADVICE: dict[type[BaseException], str] = {
    PermissionDenied: (
        "You do not have access to that project. Do not retry this call and do "
        "not try a different id — ask the analyst which project they mean."
    ),
    NotFound: (
        "That id or slug does not exist. Call search_wiki first and use the "
        "page_id it returns rather than constructing one."
    ),
}


def _advice_for(exc: BaseException) -> str:
    for kind, advice in _ADVICE.items():
        if isinstance(exc, kind):
            return advice
    return "Try a different approach, or tell the analyst what you were unable to do."


#: How much of a tool's arguments to keep on the timeline. A tool argument can
#: be a whole document; an Inspector row is not the place for it.
_MAX_ARG_CHARS = 400


def _truncate_args(args: object) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        text = value if isinstance(value, str) else repr(value)
        out[str(key)] = text if len(text) <= _MAX_ARG_CHARS else text[:_MAX_ARG_CHARS] + "…"
    return out


def _model_name(model: object) -> str | None:
    """The model id off a `ModelRequest.model`, whatever shape it arrives in.

    `ChatOpenAI` exposes `.model_name`; other integrations use `.model`. Read
    both rather than importing a provider class, for the same reason
    `classify_model_failure` matches on names: the gateway is whatever
    OpenAI-compatible endpoint the operator pointed Aleph at.
    """
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return model if isinstance(model, str) and model else None


def describe_tool_failure(tool_name: str, exc: BaseException) -> str:
    """One line the model can act on, with the tool named.

    The tool name is in the text as well as in the `ToolMessage` envelope
    because the model reads the text; a message that says only "an error
    occurred" costs a turn to diagnose.
    """
    reason = str(exc).strip() or exc.__class__.__name__
    return f"{tool_name} failed: {exc.__class__.__name__}: {reason}. {_advice_for(exc)}"


# ---------------------------------------------------------------------------
# Model-call failures: classify, back off, and fail with a code
# ---------------------------------------------------------------------------

#: Stable codes the AG-UI route reports to the browser. Deliberately few: a code
#: exists so a person can act on it, and "internal" covering everything unknown
#: is more useful than twelve codes nobody has ever seen.
FailureCode = Literal["rate_limited", "upstream_timeout", "internal"]


class AgentModelUnavailable(Exception):
    """The model could not be reached inside the configured retry budget.

    Typed rather than bare so the failure the browser is shown, the log line and
    the RUN_ERROR frame all carry the same word. An untyped exception is why
    "weirdly rate limited" and "the run errored with nothing in the log" were
    the same event described from two ends and never joined up.
    """

    def __init__(self, *, code: FailureCode, attempts: int, cause: BaseException) -> None:
        self.code = code
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"the model was unavailable after {attempts} attempt(s) [{code}]: "
            f"{type(cause).__name__}: {cause}"
        )


def classify_model_failure(exc: BaseException) -> FailureCode:
    """Map an exception to a retry decision.

    Matched on the class NAME and on the status code rather than by importing
    provider exception classes: the gateway is whatever OpenAI-compatible
    endpoint the operator pointed Aleph at, so the exception type depends on
    which client library raised it, and an import-based check silently stops
    recognising anything the next one raises.
    """
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status == 429:
        return "rate_limited"
    if status in {408, 502, 503, 504}:
        return "upstream_timeout"

    name = type(exc).__name__.lower()
    if "ratelimit" in name:
        return "rate_limited"
    if isinstance(exc, TimeoutError) or "timeout" in name or "connectionerror" in name:
        return "upstream_timeout"

    text = str(exc).lower()
    if "rate limit" in text or "too many requests" in text:
        return "rate_limited"
    return "internal"


def retry_after_seconds(exc: BaseException) -> float | None:
    """The gateway's own ``Retry-After``, if it sent one.

    A server that has told you when to come back is a better source than any
    backoff curve, and ignoring it is how a client turns one rate limit into
    several.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # a header container that does not behave like a mapping
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        # The HTTP-date form. Rare from a gateway, and guessing is worse than
        # falling back to the curve.
        return None


def retry_delay(
    attempt: int,
    exc: BaseException,
    *,
    base: float,
    ceiling: float,
    jitter: float = 0.0,
) -> float:
    """Seconds to wait before attempt ``attempt + 1``.

    ``Retry-After`` wins when present. Otherwise exponential from ``base``,
    capped at ``ceiling``, plus a caller-supplied jitter fraction — without
    jitter, N concurrent subagents that were rate limited together come back
    together, which reproduces the burst that caused it.
    """
    stated = retry_after_seconds(exc)
    if stated is not None:
        return min(stated, ceiling)
    delay = min(base * (2**attempt), ceiling)
    return delay * (1.0 + jitter)


class AlephAgentMiddleware(AgentMiddleware):
    """Wraps every tool call and every model call so a failure is survivable.

    ``sleeper`` and ``jitter`` are injectable so the retry can be tested without
    a test that actually waits thirty seconds — a backoff test that sleeps for
    real is a test people delete.
    """

    def __init__(
        self,
        *,
        settings: Any = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[], float] | None = None,
        session_maker: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self._settings_override = settings
        self._sleep = sleeper if sleeper is not None else asyncio.sleep
        self._jitter_source = jitter if jitter is not None else (lambda: random.random() * 0.25)
        # When present, every tool call is recorded as an `AgentEvent` against
        # the run the endpoint minted. Optional so the guard is testable with no
        # database — but a turn with no recorder produces no timeline, which is
        # the state WS-C3a removes. The subagents get it from `bind_runtime`.
        self._session_maker = session_maker
        self._clock = ToolEventClock()

    def _settings(self) -> Any:
        if self._settings_override is not None:
            return self._settings_override
        from aleph_api.settings import get_settings

        return get_settings()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_call = request.tool_call
        name = str(tool_call.get("name", "a tool"))
        call_id = str(tool_call.get("id") or "")
        run_id, subagent = self._attribution(request)
        await self._record(
            run_id,
            TOOL_STARTED,
            {
                "tool": name,
                "tool_call_id": call_id,
                "subagent": subagent,
                # The arguments, so a timeline shows WHAT was asked rather than
                # only that something was. Truncated: a tool argument can carry
                # a whole document.
                "args": _truncate_args(tool_call.get("args")),
            },
        )
        self._clock.start(call_id)
        try:
            result = await handler(request)
        except PermissionDenied as exc:
            # Logged at its own level and under its own event, because "the
            # agent kept hitting a project it may not touch" is a security
            # signal and must not be filed with database hiccups.
            _log.warning(
                "agent.tool.permission_denied",
                tool=name,
                tool_call_id=call_id,
                error=str(exc),
            )
            await self._record_failure(run_id, name, call_id, subagent, exc)
            return ToolMessage(
                content=describe_tool_failure(name, exc),
                tool_call_id=call_id,
                name=name,
                status="error",
            )
        except Exception as exc:
            level = "warning" if isinstance(exc, AlephError) else "exception"
            getattr(_log, level)(
                "agent.tool.failed",
                tool=name,
                tool_call_id=call_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._record_failure(run_id, name, call_id, subagent, exc)
            return ToolMessage(
                content=describe_tool_failure(name, exc),
                tool_call_id=call_id,
                name=name,
                status="error",
            )
        await self._record(
            run_id,
            TOOL_FINISHED,
            {
                "tool": name,
                "tool_call_id": call_id,
                "subagent": subagent,
                "duration_ms": self._clock.finish(call_id),
                "outcome": "ok",
            },
        )
        return result

    def _attribution(self, request: ToolCallRequest) -> tuple[Any, str]:
        """The run this tool call belongs to, and who is running it."""
        runtime = getattr(request, "runtime", None)
        return run_id_from_runtime(runtime), subagent_from_runtime(runtime)

    async def _record(self, run_id: Any, kind: str, payload: dict[str, Any]) -> None:
        if self._session_maker is None or run_id is None:
            return
        await record_tool_event(
            self._session_maker, agent_run_id=run_id, kind=kind, payload=payload
        )

    async def _record_failure(
        self, run_id: Any, name: str, call_id: str, subagent: str, exc: BaseException
    ) -> None:
        await self._record(
            run_id,
            TOOL_FAILED,
            {
                "tool": name,
                "tool_call_id": call_id,
                "subagent": subagent,
                "duration_ms": self._clock.finish(call_id),
                "outcome": "failed",
                # The exception CLASS, not the message: a message can carry a
                # user's query or a row of data, and this row is read by an
                # Inspector pane rather than by an operator with a shell.
                "error_class": type(exc).__name__,
                "error": str(exc)[:512],
            },
        )

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Retry a rate limit with real backoff; fail with a typed error.

        Three things were wrong and they compounded. Every model call gave up
        after 60 seconds with two retries; the retries were IMMEDIATE, which is
        the worst possible response to being rate limited; and the resulting
        exception killed the run outright, so "weirdly rate limited" and "the
        run errored with nothing in the log" were the same event seen from two
        ends.

        The backoff is exponential with jitter, and honours ``Retry-After`` when
        the gateway sends one — a server that has told you when to come back is
        the only source of truth better than a guess. The SDK's own
        ``max_retries`` is set to 0 in ``_gateway_chat_model`` so the two budgets
        cannot stack: a request queued behind a retry being retried again by the
        SDK multiplies the request rate exactly when the gateway can least
        afford it.

        Exhausting the budget raises ``AgentModelUnavailable``, which carries the
        stable ``rate_limited`` / ``upstream_timeout`` / ``internal`` code the
        AG-UI route reports to the browser. A bare exception is what made the
        original failure untraceable.
        """
        # Publish who this call belongs to and which model is answering it,
        # for the cost callback that runs inside `handler`. LangChain does not
        # merge `configurable` into callback `metadata`, so this is the only
        # channel that carries the run id — and `request.model` is the model
        # that ACTUALLY answers, rather than the one resolved when the process
        # started, which is what made a mid-session profile change mislabel
        # every row after it.
        # NOT `runtime.config`: LangGraph's `Runtime` has no `config` at all —
        # its own docstring says so and points at `langgraph.config.get_config()`.
        # `ToolRuntime` adds one, so the tool path can read it directly; the
        # MODEL path cannot, and reading it here silently yielded None. That is
        # how `agent_run_id` stayed NULL through a fix written to populate it,
        # and it is the same class of mistake as reading `metadata` — a channel
        # that does not carry the key.
        scope = ModelCallScope(
            agent_run_id=run_id_from_runtime(getattr(request, "runtime", None)),
            model=_model_name(getattr(request, "model", None)),
        )

        settings = self._settings()
        budget = max(1, settings.aleph_agent_max_retries)
        base = settings.aleph_agent_retry_base_delay_s
        ceiling = settings.aleph_agent_retry_max_delay_s

        last: BaseException | None = None
        for attempt in range(budget):
            try:
                with model_call_scope(scope):
                    return await handler(request)
            except Exception as exc:
                last = exc
                code = classify_model_failure(exc)
                if code == "internal" or attempt == budget - 1:
                    break
                delay = retry_delay(
                    attempt, exc, base=base, ceiling=ceiling, jitter=self._jitter_source()
                )
                _log.warning(
                    "agent.model.retrying",
                    attempt=attempt + 1,
                    of=budget,
                    delay_s=round(delay, 2),
                    code=code,
                    error=f"{type(exc).__name__}: {exc}",
                )
                await self._sleep(delay)

        assert last is not None
        code = classify_model_failure(last)
        _log.error(
            "agent.model.exhausted",
            attempts=budget,
            code=code,
            error=f"{type(last).__name__}: {last}",
        )
        raise AgentModelUnavailable(code=code, attempts=budget, cause=last) from last
