"""Mount the assistant Deep Agent as an AG-UI endpoint (Wave 2, v2 path).

Uses `ag_ui_langgraph.add_langgraph_fastapi_endpoint` (NOT the broken v1
`CopilotKitRemoteEndpoint`, which crashes on `dict_repr` against current
ag-ui-langgraph). The Node `aleph-copilot-runtime` service points a
`LangGraphHttpAgent` at this endpoint and is where A2UI tool injection
happens; the React app talks to the Node runtime.

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
) -> None:
    """Build the assistant Deep Agent and mount its AG-UI endpoint.

    Called from the FastAPI lifespan startup (not app construction): the agent's
    Postgres-backed memory `store` must be created inside the running event loop,
    and the AG-UI route is only consulted after startup completes.

    `checkpointer` carries per-thread conversation state and must be the durable
    Postgres saver in production; without it every restart drops the agent's
    history and plan.
    """
    from ag_ui_langgraph import add_langgraph_fastapi_endpoint
    from copilotkit import LangGraphAGUIAgent

    from aleph_api.copilot_agent import build_assistant_deep_agent

    graph = build_assistant_deep_agent(settings=settings, store=store, checkpointer=checkpointer)
    add_langgraph_fastapi_endpoint(
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
    )
