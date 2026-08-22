"""A conversation with the assistant becomes a recorded run.

Nothing about a chat turn was written down. There was no record that a turn
happened, which tools it called, how long they took, which subagent did what, or
how it ended. The only thing that survived was the raw message transcript in
LangGraph's checkpoint — no timings, no errors, no attribution.

Meanwhile Aleph already had the right table for exactly this (`agent_runs` +
`agent_events`) and an SSE endpoint that streams it, both used only by the
background worker jobs: `grep -rn 'AgentRun(' apps packages` finds seventeen
producers and not one of them is on the chat path.

So the backlog's claim that the Inspector "can be rebuilt on data Aleph already
has" was false — the data did not exist. This is what creates it, and it creates
it in the shape the existing read path already serves
(`GET /v1/projects/{id}/agent-events?agent_run_id=...`), so that route needs no
change at all.

**The trap this file exists to avoid.** `agent_run_id` was already a column on
`model_calls`, and it was unconditionally NULL for the whole life of the feature
because the cost callback read it from `metadata` while nothing ever put it
there. The run id here travels in `config["configurable"]`, which is the channel
deepagents forwards to subagents — the same channel the tools already read their
project scope from. Reading it from `metadata` is the specific mistake that made
the column useless, and `test_subagent_attribution` exists to catch it.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentEvent, AgentRun
from aleph_db.repos.ledger import LedgerWriter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_log = structlog.get_logger(__name__)

#: Key under which the run id travels. `configurable`, not `metadata` — see the
#: module docstring; this is the distinction that made `model_calls.agent_run_id`
#: permanently NULL.
RUN_ID_KEY = "agent_run_id"

#: Event kinds this module writes. Named here so the Inspector and the tests
#: agree with the writer rather than each carrying its own spelling.
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
TOOL_FAILED = "tool_failed"


@dataclass(frozen=True)
class ChatRun:
    """A minted run, and the project it belongs to."""

    run_id: UUID
    project_id: UUID
    correlation_id: str


class ChatRunRecorder:
    """Mints and finalizes the `AgentRun` for one chat turn.

    Every method is best-effort and says so. Recording is observability: a
    failure to write an event must never take down the turn it was observing,
    because that trades a missing log line for a broken product.
    """

    def __init__(
        self,
        *,
        session_maker: Callable[[], Any],
        project_resolver: Callable[[object], UUID | None],
        actor_id: UUID,
    ) -> None:
        self._maker = session_maker
        self._resolve_project = project_resolver
        self._actor_id = actor_id

    async def begin(self, thread_id: object) -> ChatRun | None:
        """Create the run row. Returns None when there is no project to bill it to.

        A turn with no resolvable project is not recorded rather than recorded
        against a guess: a run row naming the wrong project is worse than no row,
        because it is evidence and it is false.
        """
        project_id = self._resolve_project(thread_id)
        if project_id is None:
            return None
        run_id = uuid7()
        # Full hex, not a prefix: uuid7's leading bits are a millisecond
        # timestamp, so a truncated id collides for turns started in the same
        # window against uq_agent_runs_correlation_id.
        correlation_id = f"chat-{run_id.hex}"
        try:
            async with self._maker() as session:
                session.add(
                    AgentRun(
                        id=run_id,
                        project_id=project_id,
                        agent_kind="assistant",
                        correlation_id=correlation_id,
                        status="running",
                        started_at=utcnow(),
                        input_payload={"thread_id": str(thread_id)},
                        created_by=self._actor_id,
                    )
                )
                # The ledger write is in the same transaction as the row, per the
                # standing rule.
                await LedgerWriter(session).append(
                    project_id=project_id,
                    actor_id=self._actor_id,
                    actor_kind="user",
                    action_kind="assistant.turn",
                    target_id=run_id,
                    target_kind="agent_run",
                    payload={"thread_id": str(thread_id)},
                    trace_id=None,
                )
                await session.commit()
        except Exception:
            _log.exception("chat_run.begin_failed", thread_id=str(thread_id))
            return None
        return ChatRun(run_id=run_id, project_id=project_id, correlation_id=correlation_id)

    async def finish(self, run: ChatRun, *, status: str, error_text: str | None = None) -> None:
        """Move the run to a terminal status. Runs in a `finally`, always."""
        from sqlalchemy import select

        try:
            async with self._maker() as session:
                row = (
                    await session.execute(select(AgentRun).where(AgentRun.id == run.run_id))
                ).scalar_one_or_none()
                if row is None:
                    return
                row.status = status
                row.completed_at = utcnow()
                if error_text is not None:
                    row.error_text = error_text[:4096]
                await session.commit()
        except Exception:
            _log.exception("chat_run.finish_failed", run_id=str(run.run_id))


async def record_tool_event(
    session_maker: Callable[[], Any],
    *,
    agent_run_id: UUID,
    kind: str,
    payload: dict[str, Any],
) -> None:
    """Write one `AgentEvent`, in its own short-lived session.

    Its own session on purpose, matching `aleph_db.repos.agent_events`: the row
    has to commit immediately to be visible to the SSE poller, independent of
    whatever longer transaction the caller is in.

    Write amplification is the real risk here — a chatty turn emits an event per
    tool call, and this pattern was sized for worker phases (a handful per job),
    not for per-tool-call chat. If a turn's event count becomes a problem, batch
    here rather than dropping events.
    """
    try:
        async with session_maker() as session:
            session.add(
                AgentEvent(
                    id=uuid7(),
                    agent_run_id=agent_run_id,
                    event_kind=kind,
                    payload_jsonb=payload,
                )
            )
            await session.commit()
    except Exception:
        # Observability must never break the thing it observes.
        _log.warning("chat_run.event_failed", kind=kind, agent_run_id=str(agent_run_id))


def current_config() -> dict[str, Any] | None:
    """The running `RunnableConfig`, however this call site can reach it.

    LangGraph's `Runtime` deliberately does NOT carry `config` — its own
    docstring says so and points at `langgraph.config.get_config()`. `ToolRuntime`
    *does* add one, so the tool path can read `request.runtime.config` directly;
    the MODEL path cannot, and reading `runtime.config` there silently yields
    None, which is how `model_calls.agent_run_id` stayed NULL through a fix
    specifically written to populate it.

    `get_config()` first, because it is the documented accessor and works in
    both places. `runtime.context` second: `ag_ui_langgraph` copies the caller's
    `configurable` into the graph's `context`, so it is a real second channel
    rather than a guess.
    """
    from langgraph.config import get_config

    try:
        config = get_config()
    except Exception:
        config = None
    if isinstance(config, dict) and isinstance(config.get("configurable"), dict):
        return config
    return None


def run_id_from_runtime(runtime: object) -> UUID | None:
    """The run id, from a tool or model runtime, whichever channel carries it."""
    from_config = run_id_from_config(current_config())
    if from_config is not None:
        return from_config
    direct = run_id_from_config(getattr(runtime, "config", None))
    if direct is not None:
        return direct
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        return run_id_from_config({"configurable": context})
    return None


def subagent_from_runtime(runtime: object) -> str:
    config = current_config() or getattr(runtime, "config", None)
    named = subagent_from_config(config)
    if named != "orchestrator":
        return named
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        return subagent_from_config({"configurable": context})
    return "orchestrator"


def run_id_from_config(config: object) -> UUID | None:
    """Read the run id out of a RunnableConfig's `configurable`.

    `configurable`, not `metadata`. deepagents forwards `configurable` to
    subagents, which is what makes subagent attribution possible at all, and
    reading `metadata` instead is exactly why `model_calls.agent_run_id` was
    NULL for the whole life of that feature.
    """
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    raw = configurable.get(RUN_ID_KEY)
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def subagent_from_config(config: object) -> str:
    """Which agent is running: a subagent's name, or "orchestrator".

    deepagents names the subagent in the config's `run_name` / tags when it
    delegates. Falling back to "orchestrator" rather than to empty means the
    Inspector can always attribute work to *something*, and a distinct-count
    assertion over this field is a real check rather than a count of nulls.
    """
    if not isinstance(config, dict):
        return "orchestrator"
    configurable = config.get("configurable") or {}
    for key in ("subagent", "subagent_name"):
        value = configurable.get(key) if isinstance(configurable, dict) else None
        if isinstance(value, str) and value:
            return value
    name = config.get("run_name")
    if isinstance(name, str) and name:
        return name
    tags = config.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("subagent:"):
                return tag.split(":", 1)[1]
    return "orchestrator"


class ToolEventClock:
    """Millisecond durations, monotonic.

    A wall-clock difference can be negative across an NTP step, and a negative
    duration in an Inspector timeline reads as a bug in the Inspector.
    """

    def __init__(self) -> None:
        self._started: dict[str, float] = {}

    def start(self, tool_call_id: str) -> None:
        self._started[tool_call_id] = time.monotonic()

    def finish(self, tool_call_id: str) -> int:
        started = self._started.pop(tool_call_id, None)
        if started is None:
            return 0
        return max(0, int((time.monotonic() - started) * 1000))


# ---------------------------------------------------------------------------
# Bridging `configurable` to the cost callback
# ---------------------------------------------------------------------------
#
# LangChain does NOT merge `configurable` into the `metadata` it hands callback
# hooks — verified: `ensure_config({"configurable": {...}})["metadata"]` is `{}`.
# So `AgentCostCallbackHandler`, which reads `metadata["agent_run_id"]`, could
# never see a run id no matter who set one. That is why
# `model_calls.agent_run_id` was unconditionally NULL for the whole life of the
# column, and it is not a bug in the callback: the channel simply does not
# carry that key.
#
# `awrap_model_call` DOES see `runtime.config["configurable"]`, and it runs in
# the same task as the callback it wraps. A context variable is therefore the
# right bridge — narrow, task-scoped, and it disappears the moment the call
# returns, so a pooled worker task cannot inherit the previous call's identity.


@dataclass(frozen=True)
class ModelCallScope:
    """Who this model call belongs to, and which model is answering it."""

    agent_run_id: UUID | None
    model: str | None


_SCOPE: ContextVar[ModelCallScope | None] = ContextVar("aleph_model_call_scope", default=None)


@contextmanager
def model_call_scope(scope: ModelCallScope) -> Iterator[None]:
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_model_call_scope() -> ModelCallScope | None:
    return _SCOPE.get()
