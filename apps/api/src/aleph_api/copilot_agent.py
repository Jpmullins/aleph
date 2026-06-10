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

import pathlib
import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from aleph_wiki.index_service import IndexService

if TYPE_CHECKING:
    from langgraph.store.postgres import AsyncPostgresStore
    from psycopg import AsyncConnection
    from psycopg.rows import DictRow
    from psycopg_pool import AsyncConnectionPool
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_api.settings import Settings
    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


SYSTEM_PROMPT = """\
You are Aleph (א), the research assistant orchestrating a project's workspace \
over its compiled wiki — the primary knowledge base. You are the brain of the \
workspace: you plan, delegate the heavy work to specialist subagents, and keep \
your own replies to the analyst conversational and concise.

## How you work

**Plan first for multi-step work.** When a request needs more than one step \
(e.g. research a topic *and* turn it into a report, or analyze hypotheses \
*then* review a page), use the `write_todos` tool to lay out a short plan, then \
work the plan, updating it as you go. For a single simple step, skip the plan \
and just do it.

**Delegate the heavy work via the `task` tool.** You hold only a few light \
tools yourself; everything substantive runs in a specialist subagent whose \
isolated context keeps your own thread lean:
- `retriever` — substantive questions that need grounding. It runs the full \
wiki-first retrieval pipeline and returns a cited answer. Use `search_wiki` \
yourself only for a quick scan of what pages exist before deciding.
- `researcher` — research a topic the wiki doesn't cover. Runs in the \
background; lands a draft wiki page plus an approval proposal in the Briefs tab.
- `wiki_builder` — ingest a source URL/document, or promote an analyst note to \
a draft wiki page.
- `viz_builder` — quick charts, and full reports/decks/exports.
- `analyst` — hypotheses and Analysis of Competing Hypotheses (enumerate, \
weigh evidence, score consistency).
- `reviewer` — review/critique a wiki page (contradiction / weak-source / \
coverage-gap checks).
- `echo` — a test subagent; delegate to it only when explicitly asked to test \
delegation, and relay its reply.

When a subagent returns a render instruction (a SourceCard, ChartCard, \
HypothesisCard, ApprovalCard, …), render it exactly as instructed. For \
background work (research), tell the analyst what you kicked off and where the \
result will appear. Confirm consequential or destructive intent with the \
analyst before delegating it (e.g. creating a hypothesis, building an artifact, \
toggling a connector) — those paths are approval-gated and return an \
ApprovalCard you must render.

**Consult your skills when relevant.** You have SKILL.md skills (research, ach, \
report-authoring, wiki-style). Their names and descriptions are listed for you; \
when a request matches one, read the skill for the procedure before acting, and \
follow it.

## Voice and grounding

Ground every claim in what the wiki actually says and cite pages with \
[[Page Title]] wikilink markers. Never fabricate. When the analyst would \
benefit from a structured view (a comparison table, a chart, a hypothesis \
matrix, a claim with its confidence), render it as an interactive card rather \
than describing it in prose. Prefer the analyst's current context — the page or \
hypothesis they are viewing, provided to you — when it is relevant. When work \
lands in a specific tab (Briefs, Artifacts, Hypotheses), point the analyst \
there.

You can also list connectors and enable/disable them, and report or change the \
project's model profile, when the analyst asks about data sources or model \
settings — these are light config tools you hold directly. Enabling/disabling a \
connector is consequential and approval-gated: render the ApprovalCard it \
returns and let the analyst approve before the change applies.

## Memory

You have long-term memory at `/memories/`. At the start of substantive work, \
check `/memories/` (ls/read_file) for durable facts about this project or the \
analyst's preferences. When you learn something durable (a preference, a key \
project fact), write it to `/memories/<topic>.md` so you remember it in future \
sessions.
"""

# Stable, deterministic dev user id so ledger rows (ModelCall /
# CostLedgerEvent) written by retrieval LiteLLM calls reference a single,
# resolvable principal rather than a fresh random uuid per call.
_DEV_USER_UUID = uuid.uuid5(uuid.NAMESPACE_DNS, "dev@aleph.local")


def _dev_principal(settings: Any) -> Principal:
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
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    litellm: LiteLLMClient | None = None,
) -> None:
    _runtime["session_maker"] = session_maker
    if settings is not None:
        _runtime["settings"] = settings
    if litellm is not None:
        _runtime["litellm"] = litellm


def get_runtime() -> dict[str, Any]:
    """Public accessor for the lifespan-bound runtime (session_maker/settings/litellm).

    The cost-attribution callback reads `session_maker` from here lazily, the
    same way the tools below read it (the graph is built before `bind_runtime`).
    """
    return _runtime


def _project_id_from_thread_id(thread_id: object) -> UUID | None:
    """Parse the project UUID out of a project-prefixed thread id.

    The Node CopilotRuntime formats the thread id as `proj:<uuid>:<thread>`
    (the only channel `ag-ui-langgraph` reliably threads through to the graph).
    Returns the UUID, or None if the thread id is not project-prefixed / not a
    valid UUID.
    """
    if isinstance(thread_id, str) and thread_id.startswith("proj:"):
        parts = thread_id.split(":", 2)
        if len(parts) >= 2:
            try:
                return UUID(parts[1])
            except ValueError:
                return None
    return None


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
        return _project_id_from_thread_id(configurable.get("thread_id"))
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


async def _read_wiki_impl(query: str, config: RunnableConfig) -> str:
    """Run the full wiki-first retrieval pipeline and return a cited answer.

    Shared body of the deep wiki read: builds the dev principal, loads the
    project's ModelProfile, runs the `WikiFirstRetrievalRouter` (page selection,
    1-hop wikilink expansion, answer composition, intra-source descent) and
    returns a cited markdown answer + a coverage note. Reused by the `retriever`
    subagent's `deep_read` tool (Wave 3 T2) so the large composed body lives in
    the subagent's isolated context rather than the orchestrator's thread.
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


async def _list_hypotheses_impl(config: RunnableConfig) -> str:
    """Shared body: list the project's hypotheses with their confidence.

    Reused by the `analyst` subagent's tool (DRY). Reads the lifespan-bound
    session_maker + project scope, never the DB credentials directly.
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


async def _create_hypothesis_impl(title: str, statement: str, config: RunnableConfig) -> str:
    """Shared body: create a hypothesis (writes an Action Ledger event, rule #4).

    Reused by the `analyst` subagent's tool (DRY).
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
    except Exception as exc:
        return f"Could not create hypothesis: {exc}"
    return (
        f"Created hypothesis [{hyp_short_id}] '{hyp_title}'.\n"
        f"Render a HypothesisCard with hypothesis_id={hyp_id}, "
        f"title='{hyp_title}', confidence='{conf}', evidence_count=0."
    )


async def _add_hypothesis_evidence_impl(
    hypothesis_id: str,
    stance: str,
    evidence_kind: str,
    target_id: str,
    config: RunnableConfig,
    note: str = "",
    weight: float = 1.0,
) -> str:
    """Shared body: attach evidence to a hypothesis (writes a ledger event, rule #4).

    Reused by the `analyst` subagent's tool (DRY).
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
    except Exception as exc:
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


async def _start_research_impl(query: str, config: RunnableConfig, depth: str = "shallow") -> str:
    """Kick off background research on a topic to grow the project's wiki.

    Shared body of the research dispatch: self-calls the tested `/synthesize`
    route (connector resolution, AIQ dispatch, the result→wiki poll job; rule #3
    — never touches the DB directly) and returns immediately. Reused by the
    `researcher` subagent's `start_research` tool (Wave 3 T3) so the orchestrator
    delegates research rather than self-calling it inline.

    `depth` is "shallow" (fast, single pass, ~1 min) or "deep" (thorough,
    multi-loop, several minutes); defaults to "shallow" for responsiveness.
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
    except Exception as exc:
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


async def _pin_to_briefs_impl(
    card_kind: str,
    title: str,
    props: dict[str, Any],
    config: RunnableConfig,
) -> str:
    """Persist a catalog card to the Briefs pile (rule #3 — self-calls the
    tested `/cards/pin` route, never raw DB). The card survives the chat
    transcript and renders in the Briefs tab until the analyst unpins it."""
    import httpx

    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot pin (no project scope)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/cards/pin",
                json={"card_kind": card_kind, "title": title, "props": props},
                headers={"Authorization": "Bearer local-dev"},
            )
    except Exception as exc:
        return f"Could not pin the {card_kind}: {exc}"
    if resp.status_code >= 400:
        return f"Could not pin the {card_kind} ({resp.status_code}): {resp.text[:200]}"
    return f"Pinned '{title}' to the Briefs tab (card {resp.json()['card_id']})."


async def _ingest_source_impl(url: str, config: RunnableConfig, title: str = "") -> str:
    """Ingest a web page or document URL into the project's knowledge store.

    Shared body of source ingestion: self-calls the tested `/sources/ingest-url`
    route (fetch, normalize, chunk+embed, fold into the wiki; rule #3 — never
    touches the DB directly) and returns a SourceCard render instruction. Reused
    by the `wiki_builder` subagent's `ingest_source` tool (Wave 3 T4) so the
    orchestrator delegates ingestion rather than self-calling it inline.
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
    except Exception as exc:
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
    except Exception as exc:
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


async def _build_artifact_impl(
    title: str,
    config: RunnableConfig,
    artifact_kind: str = "report_markdown_bundle",
    wiki_page_ids: list[str] | None = None,
    csl_style: str = "apa-7",
) -> str:
    """Shared body of artifact building (report/deck/source-pack).

    Approval-gated (Wave 6): rather than building immediately, it self-calls the
    agent-actions/request route to create a pending ApprovalRequest (rule #3 —
    never touches the DB directly) and returns an ApprovalCard render
    instruction. The build only runs after the analyst clicks Approve. Reused by
    the `viz_builder` subagent's `build_artifact` tool (Wave 3 T5) so the
    orchestrator delegates artifact building rather than self-calling it inline.
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
    except Exception as exc:
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
    except Exception as exc:
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


def _psycopg_conn_string(database_url: str) -> str:
    """Convert the app's SQLAlchemy asyncpg URL to a psycopg conn string.

    `AsyncPostgresStore` connects with psycopg (not asyncpg), so strip the
    SQLAlchemy `+asyncpg` driver suffix: `postgresql+asyncpg://…` → `postgresql://…`.
    """
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def build_agent_store(
    *, database_url: str
) -> tuple[AsyncConnectionPool[AsyncConnection[DictRow]], AsyncPostgresStore]:
    """Build the Postgres-backed langgraph store for cross-session agent memory.

    Returns an *unopened* `(pool, store)` pair: the caller (the FastAPI lifespan)
    must `await pool.open()` then `await store.setup()` once at startup, and
    `await pool.close()` at shutdown. The pool is configured exactly as
    langgraph's own `AsyncPostgresStore.from_conn_string` configures it
    (autocommit, no prepared statements, dict rows). The store is constructed
    here — which requires a running event loop — so this must be called from
    within the async lifespan, not at synchronous app-construction time.
    """
    from typing import cast

    from langgraph.store.postgres import AsyncPostgresStore
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    # Mirror langgraph's own `AsyncPostgresStore.from_conn_string` pool config:
    # autocommit (the store manages its own transactions), no prepared
    # statements, and dict rows. The cast matches langgraph's own typing.
    pool = cast(
        "AsyncConnectionPool[AsyncConnection[DictRow]]",
        AsyncConnectionPool(
            _psycopg_conn_string(database_url),
            open=False,
            min_size=1,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        ),
    )
    store = AsyncPostgresStore(conn=pool)
    return pool, store


# The single agent model id, shared by the orchestrator and its subagents.
_AGENT_MODEL = "claude-sonnet-4-6"

# SKILL.md skills live in `skills/<name>/SKILL.md` alongside this module. The
# Deep Agent reads them through its backend, so a FilesystemBackend rooted here
# is routed under the in-backend `/skills/` prefix (see `_memory_backend`); the
# orchestrator is given `skills=["/skills"]` to scan that source for skills.
_SKILLS_DIR = pathlib.Path(__file__).parent / "skills"


def _gateway_chat_model(settings: Settings, *, purpose: str) -> ChatOpenAI:
    """Build a gateway-pointed `ChatOpenAI` with cost attribution (rules #2, #5).

    All agent LLM traffic (orchestrator + every subagent) is constructed here so
    it is configured identically — same model, gateway `base_url`/`api_key`,
    temperature, and `stream_usage=True` — and so each gets its own
    `AgentCostCallbackHandler` tagged with a `purpose`. The callback is attached
    ONLY to the agent model (never to `LiteLLMClient`), so the LiteLLMClient
    retrieval path is not double-counted; it writes a `ModelCall` +
    `CostLedgerEvent` per turn (rule #5) and never crashes the turn on failure.
    """
    from aleph_api.copilot_cost_callback import AgentCostCallbackHandler

    return ChatOpenAI(
        model=_AGENT_MODEL,
        base_url=settings.litellm_base_url,
        api_key=settings.insights_litellm_api_key,
        temperature=0.2,
        timeout=60,
        max_retries=2,
        callbacks=[AgentCostCallbackHandler(model=_AGENT_MODEL, purpose=purpose)],
        # A streaming OpenAI-compatible response omits the `usage` block unless
        # `stream_options.include_usage` is set. Without this the on_llm_end
        # AIMessage has `usage_metadata=None` and the cost callback has nothing
        # to record (rule #5 gap). `stream_usage=True` makes ChatOpenAI request +
        # aggregate the usage into the final chunk.
        stream_usage=True,
    )


def subagent_model(settings: Settings, name: str) -> ChatOpenAI:
    """Build a subagent's gateway `ChatOpenAI`, cost-tagged per subagent.

    Identical to the orchestrator's model but the cost callback's `purpose` is
    `assistant.subagent.<name>`, so each subagent's LLM calls write a
    `ModelCall` + `CostLedgerEvent` attributed to that subagent (rule #5).
    """
    return _gateway_chat_model(settings, purpose=f"assistant.subagent.{name}")


def build_assistant_deep_agent(*, settings: Settings, store: AsyncPostgresStore):
    """Compile the assistant Deep Agent (built once at app startup).

    Returns a LangGraph `CompiledStateGraph` suitable for
    `LangGraphAGUIAgent(graph=...)`. W3 extends this with subagents.

    `store` is the long-lived Postgres-backed langgraph store created by the
    lifespan; a `CompositeBackend` routes `/memories/` to it (cross-session
    persistence) while all other agent files stay ephemeral per-thread.
    """
    from copilotkit import CopilotKitMiddleware
    from deepagents import SubAgent, create_deep_agent
    from deepagents.backends import (
        BackendProtocol,
        CompositeBackend,
        FilesystemBackend,
        StateBackend,
        StoreBackend,
    )
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.config import get_config
    from langgraph.prebuilt.tool_node import ToolRuntime

    from aleph_api.subagents.analyst import build_analyst_subagent
    from aleph_api.subagents.researcher import build_researcher_subagent
    from aleph_api.subagents.retriever import build_retriever_subagent
    from aleph_api.subagents.reviewer import build_reviewer_subagent
    from aleph_api.subagents.viz_builder import build_viz_builder_subagent
    from aleph_api.subagents.wiki_builder import build_wiki_builder_subagent

    def _memory_namespace(_rt: object) -> tuple[str, ...]:
        """Scope persistent memory per-project so projects never share memory.

        `StoreBackend` invokes this inside the graph execution context, so the
        running config (with our `proj:<uuid>:<thread>` thread id) is available
        via langgraph's `get_config()` — the same channel the tools read project
        scope from. We parse the project UUID out of the thread id and key the
        store namespace on it: `(<project_uuid>, "memories")`. When the scope
        can't be resolved (direct caller without a project-prefixed thread id),
        fall back to a shared `("shared", "memories")` namespace rather than
        leaking one project's memory into another's default key.
        """
        project_id: UUID | None = None
        try:
            cfg = get_config()
        except Exception:
            cfg = None
        if cfg is not None:
            configurable = cfg.get("configurable") or {}
            project_id = _project_id_from_thread_id(configurable.get("thread_id"))
        if project_id is None:
            return ("shared", "memories")
        return (str(project_id), "memories")

    # Read-only host-filesystem backend for the bundled SKILL.md skills. The
    # SkillsMiddleware reads skills through the agent's backend, so routing the
    # in-backend `/skills/` prefix to this FilesystemBackend (rooted at the
    # `skills/` dir) lets `skills=["/skills"]` discover `skills/<name>/SKILL.md`.
    # `virtual_mode=True` is required for the CompositeBackend path remapping to
    # resolve correctly (with it off, the backend lists nothing under a route).
    _skills_backend = FilesystemBackend(root_dir=str(_SKILLS_DIR), virtual_mode=True)

    def _memory_backend(_rt: ToolRuntime[Any, Any]) -> BackendProtocol:
        """Route `/memories/` to the per-project StoreBackend, `/skills/` to the
        bundled SKILL.md filesystem, all else ephemeral.

        The `_rt` factory arg is still received from deepagents but is NOT passed
        to the backends: a positional `runtime` to StateBackend/StoreBackend is
        deprecated (removed in deepagents 0.7) — they obtain store/context via
        `get_store()`/`get_runtime()` now. Per-project scoping rides the explicit
        `namespace=` callable instead.
        """
        return CompositeBackend(
            default=StateBackend(),
            routes={
                "/memories/": StoreBackend(namespace=_memory_namespace),
                "/skills/": _skills_backend,
            },
        )

    # The orchestrator's OWN model. Cost is attributed to `assistant.turn` via
    # the AgentCostCallbackHandler that `_gateway_chat_model` attaches (rule #5).
    model = _gateway_chat_model(settings, purpose="assistant.turn")

    # Trivial exemplar subagent proving the in-process sync-subagent (`task`
    # tool) delegation path. Its LLM calls are cost-attributed to
    # `assistant.subagent.echo` via `subagent_model` (rule #5).
    echo_subagent: SubAgent = {
        "name": "echo",
        "description": (
            "Test subagent: echoes back a one-line confirmation. "
            "Use only when asked to test delegation."
        ),
        "system_prompt": (
            "Return a single short line confirming you ran as the echo subagent. Nothing else."
        ),
        "model": subagent_model(settings, "echo"),
    }
    # In-memory checkpointer keeps per-thread conversation state for the AG-UI
    # runtime. Cross-SESSION durability rides the `store` instead: the
    # CompositeBackend routes `/memories/` to a StoreBackend over the
    # Postgres-backed langgraph store, so memory files survive new threads and
    # process restarts. Everything else stays ephemeral (StateBackend).
    return create_deep_agent(
        model=model,
        tools=[
            search_wiki,
            list_connectors,
            set_connector_enabled,
            set_model_profile,
        ],
        system_prompt=SYSTEM_PROMPT,
        subagents=[
            echo_subagent,
            build_retriever_subagent(settings=settings),
            build_researcher_subagent(settings=settings),
            build_wiki_builder_subagent(settings=settings),
            build_viz_builder_subagent(settings=settings),
            build_analyst_subagent(settings=settings),
            build_reviewer_subagent(settings=settings),
        ],
        # Bundled SKILL.md skills (progressive disclosure): the orchestrator
        # sees each skill's name + description at startup and reads the full
        # procedure on demand. `/skills` is the in-backend source the
        # CompositeBackend routes to the FilesystemBackend above.
        skills=["/skills"],
        middleware=[CopilotKitMiddleware()],
        backend=_memory_backend,
        store=store,
        checkpointer=MemorySaver(),
    )
