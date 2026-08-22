"""Aleph's own AG-UI route, so a failed agent run says so.

What this replaces, and why it is worth owning.

`ag_ui_langgraph.add_langgraph_fastapi_endpoint` is twenty-four lines and has no
error handling of any kind::

    async def event_generator():
        async for event in request_agent.run(input_data):
            yield encoder.encode(event)

`LangGraphAgent.run()` cannot help either: it has exactly one `try:` and one
`finally:` and no `except` between them. So when the assistant breaks mid-answer
the SSE stream simply stops. The browser sees a half-written message, invents its
own error — that is the confusing "The run has already errored with RUN_ERROR"
in the console — and the only place the actual cause exists is the API
container's stderr, which is precisely why the live RUN_ERROR defect went
untraced.

Three additions, and nothing else. Aleph owns the envelope; ag-ui-langgraph
still owns the event translation, because forking that is how a wrapper becomes
a maintenance liability.

1. **An `except` around the iteration**, emitting `RunErrorEvent` as the final
   frame. The browser is told the run died, in the same channel it was reading.
2. **A terminal latch.** Upstream falls straight through from RUN_ERROR to
   RUN_FINISHED, so a client can see both and legitimately believe the run
   recovered. After a terminal event nothing else is emitted, in either
   direction.
3. **A run id shared by the frame, the log line and the response header.** A
   user reporting "it broke at 14:22" hands over an id that appears verbatim in
   the container log, instead of a timestamp and a guess.

And, since WS-MEP-6, one more thing this route owns: **which agent answers.**

The endpoint used to be handed one compiled graph, built at boot from one
globally named model profile, and every project got it until the process
restarted. So a project could change its models in Settings, the assistant would
report the change, and nothing about the assistant changed. The route now
resolves the agent per request — from the project named by the thread id, using
the same extractor `middleware/agent_scope.py` validated membership against a
moment earlier — and a resolution that FAILS is reported as a RUN_ERROR frame
like any other failure, rather than as a 500 with an empty stream.

The authorization defences are untouched and must stay that way. Membership is
decided at the HTTP boundary by `middleware/agent_scope.py` before this handler
runs; parsing the thread id here selects a profile to build with, it does not
grant anything. Both parsers are the same function on purpose.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from ag_ui.core.events import EventType, RunErrorEvent
from ag_ui.core.types import RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import Request
from fastapi.responses import StreamingResponse

from aleph_api.middleware.agent_scope import thread_project_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from uuid import UUID

    from fastapi import FastAPI

    #: `(project_id) -> an AG-UI agent`. The project may be None: a direct
    #: caller with no project-prefixed thread id still gets an assistant, built
    #: from whatever the boot default binds.
    AgentForProject = Callable[[UUID | None], Awaitable[Any]]

_log = structlog.get_logger(__name__)

#: Header carrying the id that also appears in the RUN_ERROR message and in the
#: log record. Named for Aleph rather than reusing `x-request-id`, which the
#: request-id middleware owns for the whole HTTP layer — one id per concern
#: beats two mechanisms fighting over one header.
RUN_ID_HEADER = "X-Aleph-Run-Id"

#: The recorded `agent_runs.id` for this turn, so a client — or a person with
#: curl — can open the Inspector on the exact run it just drove.
AGENT_RUN_HEADER = "X-Aleph-Agent-Run-Id"

#: Substring identifying an encoded RUN_ERROR frame. Matched on the encoded text
#: rather than re-inspecting the event, because the encoder owns the wire format
#: and the alternative is decoding what we just encoded.
RUN_ERROR_MARKER = '"RUN_ERROR"'

#: Events after which nothing may be emitted. A run ends once.
_TERMINAL = frozenset({EventType.RUN_ERROR, EventType.RUN_FINISHED})


def _event_type(event: object) -> object:
    return getattr(event, "type", None)


async def _guarded_events(
    agent: Any, input_data: RunAgentInput, encoder: EventEncoder, run_id: str
) -> AsyncIterator[str]:
    """Encode the agent's events, latch on the first terminal one, report a raise.

    The latch and the `except` are separate concerns and both are needed: the
    latch stops a *successful-looking* tail after a failure the agent already
    reported, and the `except` reports a failure the agent never got to report
    at all.
    """
    terminated = False
    try:
        async for event in agent.run(input_data):
            if terminated:
                # Upstream falls through from RUN_ERROR to RUN_FINISHED. A client
                # that sees both is entitled to conclude the run recovered.
                _log.warning(
                    "agui.event_after_terminal",
                    aleph_run_id=run_id,
                    dropped=str(_event_type(event)),
                )
                continue
            if _event_type(event) in _TERMINAL:
                terminated = True
            yield encoder.encode(event)
    except Exception as exc:
        # The diagnostic content of an agent failure used to exist only here, in
        # a container's stderr. `exc_info` keeps the traceback for the operator;
        # the id is what joins it to what the user saw.
        _log.exception(
            "agui.run_failed",
            aleph_run_id=run_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        if not terminated:
            yield encoder.encode(
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=(
                        f"The assistant run failed: {type(exc).__name__}. "
                        f"Run id {run_id} — the full traceback is in the API log "
                        f"under that id."
                    ),
                )
            )


def add_aleph_agui_endpoint(
    app: FastAPI,
    agent_for_project: AgentForProject,
    *,
    path: str,
    recorder: Any = None,
    agent_name: str = "assistant",
) -> None:
    """Mount an AG-UI SSE endpoint at `path` that reports failure.

    ``agent_for_project`` is awaited once per request with the project the
    request names, and returns the agent to run. It is a callable rather than a
    prebuilt agent because the agent depends on the project's model bindings,
    which change while the process is up; see
    `copilot_agent.assistant_agent_resolver`.

    ``recorder`` is a :class:`aleph_api.chat_runs.ChatRunRecorder` when one is
    available. It is optional so this route stays testable with no database —
    but when it is absent, a turn produces no `agent_runs` row and the Inspector
    has nothing to show, which is the state this whole workstream removes. The
    lifespan always passes one.
    """

    @app.post(path)
    async def aleph_agui_endpoint(input_data: RunAgentInput, request: Request) -> StreamingResponse:
        encoder = EventEncoder(accept=request.headers.get("accept"))
        run_id = uuid.uuid4().hex

        # Which project's models answer this turn. `thread_project_id` is the
        # extractor `middleware/agent_scope.py` already used to decide whether
        # this caller may touch this project, and that decision has been made
        # before this handler runs — `test_thread_parsers_agree` pins the two
        # together. Selecting a profile, not granting access.
        project_id = thread_project_id(getattr(input_data, "thread_id", None))

        chat_run = None
        if recorder is not None:
            chat_run = await recorder.begin(getattr(input_data, "thread_id", None))

        # Resolving can fail — an unreachable database, or a profile that binds
        # no model for a capability (`copilot_agent.NoModelBound`). Before this,
        # a build failure was an unhandled 500 with an empty stream: the exact
        # "the run just stopped" shape this module exists to remove, one layer
        # further out. It is reported in the channel the browser is reading.
        request_agent: Any = None
        resolution_error: str | None = None
        try:
            resolved = await agent_for_project(project_id)
            # Clone per request: `LangGraphAgent` keeps per-request state on
            # `self.active_run`, and sharing one instance across concurrent
            # requests corrupts it. The resolver's cache hands the SAME agent to
            # every turn with the same bindings, so this clone is now load
            # bearing for correctness, not just inherited from upstream.
            request_agent = resolved.clone()
        except Exception as exc:
            _log.exception(
                "agui.resolution_failed",
                aleph_run_id=run_id,
                project_id=str(project_id),
                error=f"{type(exc).__name__}: {exc}",
            )
            resolution_error = f"{type(exc).__name__}: {exc}"

        if request_agent is not None and chat_run is not None:
            # `LangGraphAgent.config` is a documented constructor field that
            # `clone()` copies and that the agent merges into the graph's
            # `configurable`. Setting it on the CLONE is what makes the run id
            # per-request; setting it on the shared agent would leak one
            # turn's id into every concurrent turn.
            #
            # `configurable`, not `metadata` — that distinction is why
            # `model_calls.agent_run_id` was NULL for the whole life of that
            # column, and it is what deepagents forwards to subagents.
            from aleph_api.chat_runs import RUN_ID_KEY

            existing = dict(getattr(request_agent, "config", None) or {})
            configurable = dict(existing.get("configurable") or {})
            configurable[RUN_ID_KEY] = str(chat_run.run_id)
            existing["configurable"] = configurable
            request_agent.config = existing

        async def stream() -> AsyncIterator[str]:
            status = "completed"
            error: str | None = None
            try:
                if request_agent is None:
                    status = "failed"
                    error = resolution_error
                    yield encoder.encode(
                        RunErrorEvent(
                            type=EventType.RUN_ERROR,
                            message=(
                                "The assistant could not be built for this turn: "
                                f"{resolution_error}. Run id {run_id} — the full "
                                "traceback is in the API log under that id."
                            ),
                        )
                    )
                    return
                async for frame in _guarded_events(request_agent, input_data, encoder, run_id):
                    if RUN_ERROR_MARKER in frame:
                        status = "failed"
                    yield frame
            except BaseException as exc:  # includes cancellation on a dropped client
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                # A run that stops is a run that reports — including when the
                # browser navigated away mid-stream, which is the case that
                # would otherwise leave the row `running` until the reaper.
                if recorder is not None and chat_run is not None:
                    await recorder.finish(chat_run, status=status, error_text=error)

        headers = {RUN_ID_HEADER: run_id}
        if chat_run is not None:
            headers[AGENT_RUN_HEADER] = str(chat_run.run_id)
        return StreamingResponse(stream(), media_type=encoder.get_content_type(), headers=headers)

    @app.get(f"{path}/health")
    def health() -> dict[str, Any]:
        # The name is a constant now rather than read off an agent instance:
        # there is no single agent to read it from, and inventing a resolution
        # to answer a health check would make the probe build a graph.
        return {"status": "ok", "agent": {"name": agent_name}}
