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
    from aleph_security.principal import Principal


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

When the analyst discusses competing explanations, list or create hypotheses \
and render a HypothesisCard. Confirm the statement with the analyst before \
creating one.

When the analyst shares a URL or asks to add a source, call `ingest_source` \
and render a SourceCard for the result.

When the analyst asks to draft/build a report, deck, or export, call \
`build_artifact`. Building is a consequential action, so the tool returns an \
instruction to render an ApprovalCard instead of building immediately — render \
that ApprovalCard exactly as instructed and tell the analyst the build will run \
once they approve. The finished artifact lands in the Artifacts tab.

You can list connectors and enable/disable them, and report or change the \
project's model profile, when the analyst asks about data sources or model \
settings. Enabling/disabling a connector is also consequential: the \
`set_connector_enabled` tool returns an ApprovalCard instruction — render it \
and let the analyst approve before the change applies.
"""

# Stable, deterministic dev user id so ledger rows (ModelCall /
# CostLedgerEvent) written by retrieval LiteLLM calls reference a single,
# resolvable principal rather than a fresh random uuid per call.
_DEV_USER_UUID = uuid.uuid5(uuid.NAMESPACE_DNS, "dev@aleph.local")


def _dev_principal(settings: "Any") -> "Principal":
    """Build the fixed local-dev principal for service calls from agent tools.

    Mirrors `local` auth mode: a single resolvable principal (rather than a
    fresh random uuid per call) so ledger rows reference a stable actor.
    """
    from aleph_security.principal import Principal

    return Principal(
        user_id=_DEV_USER_UUID,
        subject=getattr(settings, "local_dev_subject", "local-dev"),
        email=getattr(settings, "local_dev_email", "dev@aleph.local"),
        actor_kind="user",
    )


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

    session_maker = _runtime.get("session_maker")
    litellm = _runtime.get("litellm")
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Deep wiki reading is unavailable (no project scope on this run)."
    if litellm is None:
        return "Deep wiki reading is unavailable (LiteLLM client not bound)."
    principal = _dev_principal(settings)
    async with session_maker() as session:  # type: AsyncSession
        profile = (
            await session.execute(select(ModelProfile).where(ModelProfile.project_id == project_id))
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
async def list_hypotheses_tool(config: RunnableConfig) -> str:
    """List the current project's hypotheses with their confidence.

    Use this to recall what competing explanations the analyst is already
    tracking before proposing or creating a new one.
    """
    from aleph_hypotheses.hypothesis_service import list_hypotheses

    session_maker = _runtime.get("session_maker")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Hypotheses are unavailable (no project scope on this run)."
    async with session_maker() as session:  # type: AsyncSession
        rows = await list_hypotheses(session, project_id=project_id)
    if not rows:
        return "No hypotheses recorded yet for this project."
    lines = [
        f"- [{h.short_id}] {h.title} — confidence {getattr(h, 'confidence', 'initial')}"
        for h in rows
    ]
    return "Hypotheses:\n" + "\n".join(lines)


@tool
async def create_hypothesis_tool(title: str, statement: str, config: RunnableConfig) -> str:
    """Create a new hypothesis (a competing explanation) for the project.

    Confirm the statement with the analyst before calling this. `title` is a
    short label; `statement` is the falsifiable claim. After creating, render a
    HypothesisCard so the analyst can track and weigh evidence against it.
    """
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_hypotheses.hypothesis_service import create_hypothesis

    session_maker = _runtime.get("session_maker")
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Creating a hypothesis is unavailable (no project scope on this run)."
    try:
        async with session_maker() as session:  # type: AsyncSession
            ledger = LedgerWriter(session)
            principal = _dev_principal(settings)
            h = await create_hypothesis(
                session,
                ledger=ledger,
                principal=principal,
                project_id=project_id,
                title=title,
                statement=statement,
            )
            # Capture everything we need as plain values BEFORE commit expires
            # the ORM attributes on `h` (no `h.<attr>` access after the block).
            conf = getattr(h, "confidence", "initial")
            hyp_id = str(h.id)
            hyp_title = h.title
            hyp_short_id = h.short_id
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        return f"Could not create hypothesis: {exc}"
    return (
        f"Created hypothesis [{hyp_short_id}] '{hyp_title}'.\n"
        f"Render a HypothesisCard with hypothesis_id={hyp_id}, "
        f"title='{hyp_title}', confidence='{conf}', evidence_count=0."
    )


@tool
async def add_hypothesis_evidence_tool(
    hypothesis_id: str,
    stance: str,
    evidence_kind: str,
    target_id: str,
    config: RunnableConfig,
    note: str = "",
    weight: float = 1.0,
) -> str:
    """Attach a piece of evidence to a hypothesis.

    `stance` is one of supports / contradicts / contextualizes. `evidence_kind`
    is one of claim / source_page / chunk / finding / other_hypothesis.
    `target_id` is the UUID of the referenced entity. Adding evidence may shift
    the hypothesis's confidence; re-render its HypothesisCard afterward.
    """
    from sqlalchemy import func, select

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_hypotheses.hypothesis_service import add_evidence, get_hypothesis
    from aleph_hypotheses.models import HypothesisEvidence

    session_maker = _runtime.get("session_maker")
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Adding evidence is unavailable (no project scope on this run)."
    try:
        hyp_uuid = UUID(hypothesis_id)
        tgt_uuid = UUID(target_id)
    except ValueError:
        return "hypothesis_id and target_id must be valid UUIDs."
    try:
        async with session_maker() as session:  # type: AsyncSession
            ledger = LedgerWriter(session)
            principal = _dev_principal(settings)
            await add_evidence(
                session,
                ledger=ledger,
                principal=principal,
                hypothesis_id=hyp_uuid,
                stance=stance,
                evidence_kind=evidence_kind,
                target_id=tgt_uuid,
                weight=weight,
                note=note,
            )
            # Read the (flushed, in-transaction) hypothesis + evidence count
            # BEFORE commit, and capture everything as plain values so no
            # expired ORM attribute is touched after the block closes.
            h = await get_hypothesis(session, project_id=project_id, hypothesis_id=hyp_uuid)
            evidence_count = (
                await session.execute(
                    select(func.count())
                    .select_from(HypothesisEvidence)
                    .where(HypothesisEvidence.hypothesis_id == hyp_uuid)
                )
            ).scalar_one()
            if h is None:
                hyp_short_id = hyp_title = hyp_id = None
                conf = "initial"
            else:
                hyp_short_id = h.short_id
                hyp_title = h.title
                hyp_id = str(h.id)
                conf = getattr(h, "confidence", "initial")
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        return f"Could not add evidence: {exc}"
    if hyp_id is None:
        return (
            f"Recorded {stance} evidence, but could not re-load hypothesis "
            f"{hypothesis_id} to report its updated state."
        )
    return (
        f"Recorded {stance} evidence on [{hyp_short_id}] '{hyp_title}' "
        f"(confidence now '{conf}').\n"
        f"Re-render the HypothesisCard with hypothesis_id={hyp_id}, "
        f"title='{hyp_title}', confidence='{conf}', evidence_count={evidence_count}."
    )


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
    base = settings.aleph_self_url
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


@tool
async def ingest_source(url: str, config: RunnableConfig, title: str = "") -> str:
    """Ingest a web page or document URL into the project's knowledge store.

    Fetches the URL, normalizes, chunks+embeds, folds into the wiki. Render a
    SourceCard for the result. Use when the analyst shares a link or asks to add
    a source.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot ingest (no project scope)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/sources/ingest-url",
                json={"url": url, "title": title},
                headers={"Authorization": "Bearer local-dev"},
            )
    except Exception as exc:  # noqa: BLE001
        return f"Could not ingest {url}: {exc}"
    if resp.status_code >= 400:
        return f"Could not ingest {url} ({resp.status_code}): {resp.text[:200]}"
    b = resp.json()
    return (
        f"Ingesting {url} (source {b['source_id']}, status {b['status']}). "
        f"Render a SourceCard with source_id={b['source_id']}, short_id='', "
        f"title='{title or url}', url='{url}', status='{b['status']}'."
    )


async def _request_agent_action(
    *,
    settings: Any,
    project_id: UUID,
    tool: str,
    args: dict[str, Any],
    title: str,
    summary: str,
) -> str:
    """Create a pending approval for a consequential agent action.

    Self-calls the agent-actions/request route (which persists the tool + args
    server-side as a pending ApprovalRequest) and returns an instruction for the
    agent to render an ApprovalCard addressing that request. The effect only runs
    when the analyst clicks Approve (→ /cards/actions → ActionRouter._approve).
    """
    import httpx

    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/agent-actions/request",
                json={"tool": tool, "args": args, "title": title, "summary": summary},
                headers={"Authorization": "Bearer local-dev"},
            )
    except Exception as exc:  # noqa: BLE001
        return f"Could not request approval for {tool}: {exc}"
    if resp.status_code >= 400:
        return f"Could not request approval ({resp.status_code}): {resp.text[:200]}"
    request_id = resp.json()["request_id"]
    return (
        f"This is a consequential action, so it needs the analyst's approval "
        f"before it runs. Render an ApprovalCard with target_id={request_id}, "
        "target_kind='agent_action', "
        f"title='{title}', summary='{summary}', approve_action='approve', "
        "reject_action='reject', severity='medium'. The action will run only "
        "when the analyst clicks Approve."
    )


@tool
async def build_artifact(
    title: str,
    config: RunnableConfig,
    artifact_kind: str = "report_markdown_bundle",
    wiki_page_ids: list[str] | None = None,
    csl_style: str = "apa-7",
) -> str:
    """Build a product artifact (report/deck/source-pack) from approved wiki pages.

    This is a consequential action, so it is **approval-gated**: instead of
    building immediately, it creates a pending approval and asks you to render an
    ApprovalCard. The build only runs after the analyst clicks Approve.
    `artifact_kind` is one of report_pdf, report_docx, report_markdown_bundle,
    source_pack, deck_pdf. Use when the analyst asks to draft/build a report,
    deck, or export.
    """
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot build (no project scope)."
    args = {
        "title": title,
        "artifact_kind": artifact_kind,
        "template_name": artifact_kind,
        "csl_style": csl_style,
        "wiki_page_ids": wiki_page_ids or [],
        "dataset_version_ids": [],
    }
    return await _request_agent_action(
        settings=settings,
        project_id=project_id,
        tool="build_artifact",
        args=args,
        title=f"Build artifact: {title}",
        summary=(
            f"Build a {artifact_kind} artifact titled '{title}' from "
            f"{len(wiki_page_ids or [])} selected wiki page(s)."
        ),
    )


@tool
async def list_connectors(config: RunnableConfig) -> str:
    """List the available data-source connectors and their enabled state.

    Use this when the analyst asks what data sources / connectors are
    configured, or before enabling/disabling one (to get its connector id).
    Read-only.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Connectors are unavailable (no project scope on this run)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            connectors_resp = await client.get(
                f"{base}/v1/connectors",
                headers={"Authorization": "Bearer local-dev"},
            )
            bindings_resp = await client.get(
                f"{base}/v1/projects/{project_id}/connectors/bindings",
                headers={"Authorization": "Bearer local-dev"},
            )
    except Exception as exc:  # noqa: BLE001
        return f"Could not list connectors: {exc}"
    if connectors_resp.status_code >= 400:
        return (
            f"Could not list connectors ({connectors_resp.status_code}): "
            f"{connectors_resp.text[:200]}"
        )
    connectors = connectors_resp.json()
    if not connectors:
        return "No connectors are registered."
    # Map connector_id -> enabled from the project's bindings (a connector with
    # no binding falls back to its enabled_by_default).
    bindings = bindings_resp.json() if bindings_resp.status_code < 400 else []
    enabled_by_id: dict[str, bool] = {str(b["connector_id"]): bool(b["enabled"]) for b in bindings}
    lines = []
    for c in connectors:
        state = enabled_by_id.get(str(c["id"]), bool(c.get("enabled_by_default", False)))
        lines.append(
            f"- {c['name']} ({'enabled' if state else 'disabled'}) "
            f"· kind={c['kind']} · id={c['id']}"
        )
    return "Connectors:\n" + "\n".join(lines)


@tool
async def set_connector_enabled(connector_id: str, enabled: bool, config: RunnableConfig) -> str:
    """Enable or disable a data-source connector for the current project.

    This is a consequential action, so it is **approval-gated**: instead of
    toggling immediately, it creates a pending approval and asks you to render an
    ApprovalCard. The connector only changes after the analyst clicks Approve.
    `connector_id` is the connector's UUID (call `list_connectors` first to get
    it). `enabled` is true to turn it on, false to turn it off.
    """
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Setting a connector is unavailable (no project scope on this run)."
    try:
        cid = UUID(connector_id)
    except ValueError:
        return (
            f"connector_id must be a valid UUID (got '{connector_id}'). "
            "Call list_connectors to get ids."
        )
    verb = "Enable" if enabled else "Disable"
    return await _request_agent_action(
        settings=settings,
        project_id=project_id,
        tool="set_connector_enabled",
        args={"connector_id": str(cid), "enabled": enabled, "config_jsonb": {}},
        title=f"{verb} connector",
        summary=f"{verb} data-source connector {cid} for this project.",
    )


@tool
async def set_model_profile(profile_name: str, config: RunnableConfig) -> str:
    """Report or change the project's model profile by name.

    `profile_name` is one of "aleph-dev" or "aleph-production". Reads the
    project's current profile and the available named templates. Switching the
    project's profile by name is not yet exposed as an endpoint, so this reports
    the current and available profiles rather than performing a switch.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Model profile is unavailable (no project scope on this run)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            current_resp = await client.get(
                f"{base}/v1/projects/{project_id}/model-profile",
                headers={"Authorization": "Bearer local-dev"},
            )
            templates_resp = await client.get(
                f"{base}/v1/model-profile-templates",
                headers={"Authorization": "Bearer local-dev"},
            )
    except Exception as exc:  # noqa: BLE001
        return f"Could not read the model profile: {exc}"
    current_name = "unknown"
    if current_resp.status_code < 400:
        current_name = current_resp.json().get("name", "unknown")
    available = ["aleph-dev", "aleph-production"]
    if templates_resp.status_code < 400:
        names = [t.get("name") for t in templates_resp.json() if t.get("name")]
        if names:
            available = names
    return (
        f"The project's current model profile is '{current_name}'. "
        f"Available profiles: {', '.join(available)}. "
        f"Switching the profile by name (you asked for '{profile_name}') is not "
        "yet exposed as an endpoint, so I can't change it from here yet. Per-"
        "capability binding edits go through the model-profile PATCH route, but "
        "there is no named-template switch route to call."
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
        tools=[
            search_wiki,
            read_wiki,
            list_hypotheses_tool,
            create_hypothesis_tool,
            add_hypothesis_evidence_tool,
            start_research,
            ingest_source,
            build_artifact,
            list_connectors,
            set_connector_enabled,
            set_model_profile,
        ],
        system_prompt=SYSTEM_PROMPT,
        middleware=[CopilotKitMiddleware()],
        checkpointer=MemorySaver(),
    )
