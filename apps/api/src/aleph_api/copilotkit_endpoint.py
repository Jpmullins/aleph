"""Mount the assistant Deep Agent as an AG-UI endpoint (Wave 2, v2 path).

Mounted through `aleph_api.agui_endpoint`, not
`ag_ui_langgraph.add_langgraph_fastapi_endpoint`. The upstream helper has no
error handling at all, so an agent that broke mid-answer produced a stream that
just stopped: the browser showed a half-written message and invented its own
error, and the actual cause existed only in this container's stderr. Aleph owns
the envelope; ag-ui-langgraph still owns the event translation. (Not the broken
v1 `CopilotKitRemoteEndpoint` either, which crashes on `dict_repr`.)

The Node `aleph-copilot-runtime` service points a `LangGraphHttpAgent` at this
endpoint and is where A2UI tool injection happens; the React app talks to the
Node runtime.

Endpoint: POST /copilotkit/agent/assistant  (AG-UI RunAgentInput → SSE).
Auth: `/copilotkit` is in the middleware self-auth prefix list (local
mode → dev principal); the Node runtime forwards the project scope via
RunnableConfig.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from langgraph.store.postgres import AsyncPostgresStore

    from aleph_api.settings import Settings

_AGENT_PATH = "/copilotkit/agent/assistant"


def setup_copilotkit(
    app: FastAPI,
    *,
    settings: Settings,
    store: AsyncPostgresStore,
    checkpointer: object = None,
    session_maker: object = None,
) -> None:
    """Build the assistant Deep Agent and mount its AG-UI endpoint.

    Called from the FastAPI lifespan startup (not app construction): the agent's
    Postgres-backed memory `store` must be created inside the running event loop,
    and the AG-UI route is only consulted after startup completes.

    `checkpointer` carries per-thread conversation state and must be the durable
    Postgres saver in production; without it every restart drops the agent's
    history and plan.
    """
    from copilotkit import LangGraphAGUIAgent

    from aleph_api.agui_endpoint import add_aleph_agui_endpoint
    from aleph_api.chat_runs import ChatRunRecorder
    from aleph_api.copilot_agent import (
        _dev_actor_id,
        _project_id_from_thread_id,
        build_assistant_deep_agent,
    )

    graph = build_assistant_deep_agent(settings=settings, store=store, checkpointer=checkpointer)
    # Every turn becomes an `agent_runs` row. Nothing about a chat turn was
    # written down before this: no record that it happened, which tools it
    # called, how long they took, which subagent did what, or how it ended —
    # while `agent_runs` and the `/agent-events` SSE route already existed and
    # were used only by the worker jobs.
    recorder = (
        ChatRunRecorder(
            session_maker=session_maker,
            project_resolver=_project_id_from_thread_id,
            actor_id=_dev_actor_id(),
        )
        if session_maker is not None
        else None
    )
    add_aleph_agui_endpoint(
        app,
        LangGraphAGUIAgent(
            name="assistant",
            description=(
                "Aleph research assistant — answers from the project's "
                "compiled wiki, cites pages, and renders interactive A2UI cards."
            ),
            graph=graph,
        ),
        path=_AGENT_PATH,
        recorder=recorder,
    )
