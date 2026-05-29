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

import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from aleph_wiki.index_service import IndexService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_api.settings import Settings
    from aleph_models.client import LiteLLMClient


SYSTEM_PROMPT = """\
You are Aleph's research assistant, operating over a project's compiled \
wiki — the primary knowledge base.

Use `search_wiki` for a quick scan of what pages exist; use `read_wiki` to \
actually answer a question with a cited, composed answer. \
Ground every claim in what the wiki actually says and cite pages with \
[[Page Title]] wikilink markers. Never fabricate.

If the wiki does not cover the question (search_wiki returns nothing relevant), \
offer to research it and — when the analyst agrees, or when they explicitly ask \
to research/look into/synthesize a topic — call `start_research` with a focused \
query. Research runs in the background and lands a draft wiki page plus an \
approval proposal in the Briefs tab. After starting research, briefly tell the \
analyst what you kicked off and that the proposal will appear in Briefs.

When the analyst would benefit from a structured view (a comparison \
table, a chart of figures, a hypothesis matrix, a claim with its \
confidence), render it as an interactive card rather than describing it \
in prose. Prefer the analyst's current context (the page or hypothesis \
they are viewing, provided to you) when relevant.
"""

# Stable, deterministic dev user id so ledger rows (ModelCall /
# CostLedgerEvent) written by retrieval LiteLLM calls reference a single,
# resolvable principal rather than a fresh random uuid per call.
_DEV_USER_UUID = uuid.uuid5(uuid.NAMESPACE_DNS, "dev@aleph.local")

# Runtime dependencies bound by lifespan (the graph is built before the
# session_maker exists, so the tools read them here at call time).
_runtime: dict[str, Any] = {"session_maker": None, "settings": None, "litellm": None}


def bind_runtime(
    *,
    session_maker: "async_sessionmaker[AsyncSession]",
    settings: "Settings | None" = None,
    litellm: "LiteLLMClient | None" = None,
) -> None:
    _runtime["session_maker"] = session_maker
    if settings is not None:
        _runtime["settings"] = settings
    if litellm is not None:
        _runtime["litellm"] = litellm


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


@tool
async def read_wiki(query: str, config: RunnableConfig) -> str:
    """Read the wiki in depth to answer a question with citations.

    Use this (not search_wiki) when the analyst asks a substantive question
    that needs a composed, cited answer. Runs the full wiki-first retrieval
    pipeline: page selection, 1-hop wikilink expansion, answer composition,
    and intra-source descent. Returns a cited markdown answer + coverage note.
    """
    from uuid import uuid4

    from sqlalchemy import select

    from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter
    from aleph_db.models.model_profile import ModelProfile
    from aleph_security.principal import Principal

    session_maker = _runtime.get("session_maker")
    litellm = _runtime.get("litellm")
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Deep wiki reading is unavailable (no project scope on this run)."
    if litellm is None:
        return "Deep wiki reading is unavailable (LiteLLM client not bound)."
    principal = Principal(
        user_id=_DEV_USER_UUID,
        subject=settings.local_dev_subject if settings is not None else "local-dev",
        email=settings.local_dev_email if settings is not None else "dev@aleph.local",
        actor_kind="user",
    )
    async with session_maker() as session:  # type: AsyncSession
        profile = (
            await session.execute(
                select(ModelProfile).where(ModelProfile.project_id == project_id)
            )
        ).scalar_one_or_none()
    if profile is None:
        return "No model profile bound to this project; cannot read the wiki."
    router = WikiFirstRetrievalRouter(session_maker=session_maker, litellm=litellm)
    result = await router.retrieve(
        principal=principal,
        project_id=project_id,
        thread_id=uuid4(),
        query=query,
        prior_messages=[],
        profile=profile,
        agent_run_id=None,
    )
    coverage = getattr(result, "coverage_judgment", "ok")
    body = getattr(result, "composed_body_md", "") or "(the composer returned no body)"
    return f"{body}\n\n_(coverage: {coverage})_"


@tool
async def start_research(query: str, config: RunnableConfig, depth: str = "shallow") -> str:
    """Kick off background research on a topic to grow the project's wiki.

    Use this when the wiki does not yet cover what the analyst is asking about,
    or when they explicitly ask to research/synthesize a topic. `depth` is
    "shallow" (fast, single pass, ~1 min) or "deep" (thorough, multi-loop,
    several minutes). Default to "shallow" for responsiveness; only use "deep"
    when the analyst explicitly asks for a thorough/exhaustive/deep dive.
    Research runs in the background via the AIQ researcher; when it finishes it
    lands a draft wiki page and a proposal in the Briefs tab for approval.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Research is unavailable (no project scope on this run)."
    depth = depth if depth in ("shallow", "deep") else "shallow"
    # Self-call the synthesize endpoint so we reuse the full, tested dispatch
    # path (connector resolution, AIQ dispatch, the result→wiki poll job).
    base = getattr(settings, "aleph_self_url", None) or "http://localhost:8000"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/synthesize",
                json={"topic": query, "depth": depth},
                headers={"Authorization": "Bearer local-dev"},
            )
    except Exception as exc:  # noqa: BLE001
        return f"Could not start research: {exc}"
    if resp.status_code >= 400:
        return f"Research could not start ({resp.status_code}): {resp.text[:200]}"
    body = resp.json()
    if not body.get("dispatched"):
        return (
            f"Queued {depth} research on '{query}', but the research service "
            "did not accept the dispatch — it may be unavailable right now."
        )
    return (
        f"Started {depth} research on '{query}'. It runs in the background "
        "(~1 minute); when it finishes I'll have a draft wiki page and an "
        "approval proposal waiting in the Briefs tab. Open Briefs to review it."
    )


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
        tools=[search_wiki, read_wiki, start_research],
        system_prompt=SYSTEM_PROMPT,
        middleware=[CopilotKitMiddleware()],
        checkpointer=MemorySaver(),
    )
