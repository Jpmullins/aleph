"""CopilotKit-native assistant **Deep Agent** (Wave 2 / converges with W3).

A `deepagents.create_deep_agent` graph exposed over AG-UI via
`ag_ui_langgraph.add_langgraph_fastapi_endpoint`. Runs in-process in
aleph-api and streams tokens, tool calls, and shared state to the
browser over the Node CopilotRuntime → `/copilotkit`.

Per CLAUDE.md rule #2 (relaxed Wave 2): `ChatOpenAI` is permitted ONLY
pointed at the Insights LiteLLM gateway. `CopilotKitMiddleware` enables
frontend tools (`useFrontendTool`) + context (`useAgentContext`).

The graph is built once at startup. Per-request scope (which project's
wiki to search) arrives via the LangGraph `RunnableConfig` the runtime
forwards — read in the `search_wiki` tool. `session_maker` is supplied
by lifespan through `bind_runtime()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from aleph_wiki.index_service import IndexService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_api.settings import Settings


SYSTEM_PROMPT = """\
You are Aleph's research assistant, operating over a project's compiled \
wiki — the primary knowledge base.

ALWAYS call `search_wiki` first to find relevant pages before answering. \
Ground every claim in what the wiki actually says and cite pages with \
[[Page Title]] wikilink markers. If the wiki does not cover the question, \
say so plainly and suggest running `/synthesize` to research and grow the \
wiki — never fabricate.

When the analyst would benefit from a structured view (a comparison \
table, a chart of figures, a hypothesis matrix, a claim with its \
confidence), render it as an interactive card rather than describing it \
in prose. Prefer the analyst's current context (the page or hypothesis \
they are viewing, provided to you) when relevant.
"""

# Runtime dependencies bound by lifespan (the graph is built before the
# session_maker exists, so the tool reads it here at call time).
_runtime: dict[str, Any] = {"session_maker": None}


def bind_runtime(*, session_maker: "async_sessionmaker[AsyncSession]") -> None:
    _runtime["session_maker"] = session_maker


def _project_id_from_config(config: RunnableConfig | None) -> UUID | None:
    """Resolve the project scope for this agent run.

    `ag-ui-langgraph` only threads `thread_id` into `configurable` (it
    ignores request-level config and routes `forwarded_props` elsewhere),
    so the reliable channel is a project-prefixed thread id of the form
    `proj:<uuid>:<thread>`, which the Node CopilotRuntime formats. We also
    accept an explicit `projectId`/`project_id` in configurable/metadata
    for direct callers (curl, tests).
    """
    if not config:
        return None
    configurable = config.get("configurable") or {}
    raw = (
        configurable.get("projectId")
        or configurable.get("project_id")
        or (config.get("metadata") or {}).get("projectId")
    )
    if not raw:
        thread_id = configurable.get("thread_id") or ""
        if isinstance(thread_id, str) and thread_id.startswith("proj:"):
            parts = thread_id.split(":", 2)
            if len(parts) >= 2:
                raw = parts[1]
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


@tool
async def search_wiki(query: str, config: RunnableConfig, top_k: int = 6) -> str:
    """Search the current project's compiled wiki for pages relevant to a query.

    Returns the top matching wiki pages (title, kind, summary, relevance
    score). Call this before answering any question about the project's
    subject matter.
    """
    session_maker = _runtime.get("session_maker")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Wiki search is unavailable (no project scope on this run)."
    async with session_maker() as session:  # type: AsyncSession
        svc = IndexService(session)
        hits = await svc.select_pages(
            project_id=project_id, query=query, top_k=max(1, min(top_k, 20))
        )
    if not hits:
        return "No wiki pages matched. The wiki may not cover this topic yet."
    lines = []
    for h in hits:
        stub = " (stub)" if h.is_stub else ""
        lines.append(
            f"- [[{h.title}]]{stub} · {h.page_kind} · score={h.score:.2f}\n"
            f"  {h.summary or '(no summary)'}"
        )
    return "Relevant wiki pages:\n" + "\n".join(lines)


def build_assistant_deep_agent(*, settings: "Settings"):
    """Compile the assistant Deep Agent (built once at app startup).

    Returns a LangGraph `CompiledStateGraph` suitable for
    `LangGraphAGUIAgent(graph=...)`. W3 extends this with subagents.
    """
    from copilotkit import CopilotKitMiddleware
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import MemorySaver

    model = ChatOpenAI(
        model="claude-sonnet-4-6",
        base_url=settings.litellm_base_url,
        api_key=settings.insights_litellm_api_key,
        temperature=0.2,
        timeout=60,
        max_retries=2,
    )
    # In-memory checkpointer keeps per-thread state for the AG-UI runtime.
    # (A Postgres checkpointer is the production upgrade; memory is fine for
    # the in-process single-replica dev/local runtime.)
    return create_deep_agent(
        model=model,
        tools=[search_wiki],
        system_prompt=SYSTEM_PROMPT,
        middleware=[CopilotKitMiddleware()],
        checkpointer=MemorySaver(),
    )
