"""CopilotKit-native assistant **Deep Agent** (Wave 2 / converges with W3).

A `deepagents.create_deep_agent` graph exposed over AG-UI via
`aleph_api.agui_endpoint`. Runs in-process in aleph-api and streams
tokens, tool calls, and shared state to the browser over the Node
CopilotRuntime → `/copilotkit`.

Per CLAUDE.md rule #2 (relaxed Wave 2): `ChatOpenAI` is permitted ONLY
pointed at the Insights LiteLLM gateway. `CopilotKitMiddleware` enables
frontend tools (`useFrontendTool`) + context (`useAgentContext`).

The graph is built once at startup. Per-request scope (which project's
wiki to search) arrives via the LangGraph `RunnableConfig` the runtime
forwards — read in the `search_wiki` tool. `session_maker` is supplied
by lifespan through `bind_runtime()`.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from aleph_core.errors import PermissionDenied
from aleph_core.ids import uuid7
from aleph_security.request_context import current_principal, require_project_access
from aleph_security.roles import ProjectRole
from aleph_wiki.index_service import IndexService
from aleph_wiki.lint import lint_wiki
from aleph_wiki.schema_service import SchemaService

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
benefit from a structured view, render it as an interactive card via render_a2ui \
rather than describing it in prose. Prefer Aleph's polished domain cards over \
hand-composed A2UI primitives when one fits:
- a **TableCard** for a taxonomy, comparison, or any row/column data (e.g. \
"the kinds of distillation" → a table of Kind / Category / Description);
- a **ChartCard** for quantitative data (or delegate to viz_builder's make_chart);
- a **HypothesisCard**/matrix for competing hypotheses, a **ClaimCard** for a \
single cited assertion.
Emit the card via your render_a2ui tool using the exact component name above. \
Prefer the analyst's current context — the page or \
hypothesis they are viewing, provided to you — when it is relevant. When work \
lands in a specific tab (Briefs, Library, Hypotheses), point the analyst \
there.

## Driving the workspace (eyes + hands)

You can see and steer the analyst's workspace. Shared state tells you the active \
tab, the open wiki page, and the analyst's current selection — so "summarize \
this page" needs nothing named. You can drive the UI: `focus_tab` switches the \
right-panel tab, `open_page` opens a wiki page (by id or slug) in the reader, \
and `highlight_claim` highlights a claim in the open page. You can also compose: \
`pin_to_brief` keeps a card in Briefs, `compose_dossier` groups pages/cards into \
one read-only dossier card, and `spotlight` sorts a Briefs card to the top. Use \
these to land the analyst on exactly what you're talking about rather than only \
describing it.

The wiki is governed by a schema, and you must orient on it before you write. \
`wiki_schema` gives the domain, the category list, the controlled tag taxonomy \
and the page thresholds; a page carrying a tag or category not in that schema \
is rejected at commit time, so read it first rather than inventing vocabulary. \
If the schema is still the shipped default and the corpus is plainly about \
something else, say so and offer to derive one from the pages that exist.

Four page statuses, and two of them are queues for a PERSON — they are not the \
same queue. `stub` is a red link nobody wrote and nobody proposed; it is not \
work. `planned` is a title that earned a page by being cited enough — a queue \
for WRITING, allowed to be long. `draft` has content and is a queue for REVIEW. \
`approved` is settled. Never describe stubs or planned pages as drafts or as \
awaiting approval, and never count them into a review backlog.

When the analyst asks how the wiki is doing, what needs review, or which links \
are broken, call `wiki_curation_status` for counts, and `wiki_lint_report` when \
they want to know what is actually WRONG — broken links, orphans, uncategorised \
pages, near-duplicates that should be merged, pages nobody has judged. Lint \
findings are ordered worst-first; lead with what breaks navigation, not with \
style. Then point them to the Wiki tab (where they can approve drafts and \
repair links). You can also list connectors and enable/disable \
them, and report or change the project's model profile, when the analyst asks \
about data sources or model settings — these are light config tools you hold \
directly. Enabling/disabling a \
connector is consequential and approval-gated: render the ApprovalCard it \
returns and let the analyst approve before the change applies.

When the analyst asks how the system is doing, why something failed or stalled, \
or whether errors are happening, call `diagnose_platform` — it reads the \
platform's own Langfuse traces and reports trace volume plus the most recent \
errors. Use it to ground answers about platform health in real telemetry \
rather than guessing.

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


def _dev_actor_id() -> uuid.UUID:
    """Who a recorded chat turn is attributed to.

    Aleph runs single-user in `local` mode, so this is the JIT-provisioned dev
    principal — the same identity the auth middleware synthesizes, so the
    `agent_runs.created_by` on a chat turn matches the `created_by` on
    everything else that turn writes. `WS-D2` replaces it with the real
    principal once the cost path carries one.
    """
    return _DEV_USER_UUID


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
    agent_bindings: dict[str, Any] | None = None,
    pricing: Any = None,
) -> None:
    _runtime["session_maker"] = session_maker
    if settings is not None:
        _runtime["settings"] = settings
    if litellm is not None:
        _runtime["litellm"] = litellm
    if agent_bindings is not None:
        _runtime["agent_bindings"] = agent_bindings
    if pricing is not None:
        # The kernel's PRICING object, not a copy. `refresh_pricing` merges into
        # it in place, so holding the object is what lets newly discovered rates
        # reach the agent's cost path without a restart — the agent path used to
        # fabricate its own empty table and memoise it, so 100% of assistant
        # traffic recorded pricing_source="unknown" forever.
        _runtime["pricing"] = pricing


def get_runtime() -> dict[str, Any]:
    """Public accessor for the lifespan-bound runtime (session_maker/settings/litellm).

    The cost-attribution callback reads `session_maker` from here lazily, the
    same way the tools below read it (the graph is built before `bind_runtime`).
    """
    return _runtime


# ---------------------------------------------------------------------------
# Self-call auth (A4)
# ---------------------------------------------------------------------------
# The in-process assistant reaches state ONLY by self-calling its own tested
# API routes (rule #3). Those calls historically carried a hardcoded local-dev
# bearer sentinel, which the auth middleware honors ONLY in local auth mode — so
# without one every self-call 401s. `_self_headers` mints a real short-lived
# HS256 agent token instead, which the middleware verifies in BOTH modes.

# A self-call completes in seconds; the token never needs to outlive the
# request that mints it.
_SELF_CALL_TTL_SECONDS = 300


async def _resolve_self_call_user_id(settings: Any) -> UUID:
    """Resolve the `User` id the assistant's in-process self-calls act as.

    The AG-UI endpoint threads only project scope onto an agent run (no user
    identity), so self-calls act as the same service user the old local-dev
    bearer sentinel resolved to in local mode — looked up by
    subject so the minted token's `user_id` references a real `User` row. The
    auth middleware (`verify_agent_token`) refuses a token whose user is
    unknown, and the project-scope gate keys the role on this id, so it must be
    a real project-member user (the dev user is, exactly as under the sentinel).
    """
    from sqlalchemy import select

    from aleph_db.models.identity import User

    session_maker = _runtime.get("session_maker")
    subject = getattr(settings, "local_dev_subject", "local-dev")
    if session_maker is not None:
        async with session_maker() as session:
            uid = (
                await session.execute(select(User.id).where(User.subject == subject))
            ).scalar_one_or_none()
        if uid is not None:
            return uid
    return _DEV_USER_UUID


async def _self_headers(project_id: UUID, *, settings: Any) -> dict[str, str]:
    """Mint the Authorization header for an in-process API self-call (A4).

    Replaces the hardcoded local-dev bearer sentinel with a
    real short-lived HS256 agent token scoped to the acting project + a fresh
    agent_run_id. The sentinel authenticated ONLY in local auth mode; a minted
    agent token is verified by the auth middleware, so these self-calls do not 401.
    """
    from aleph_security.agent_token import mint_agent_token

    user_id = await _resolve_self_call_user_id(settings)
    agent_run_id = uuid7()
    token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=f"assistant-selfcall-{agent_run_id.hex}",
        ttl_seconds=_SELF_CALL_TTL_SECONDS,
    )
    return {"Authorization": f"Bearer {token}"}


def _agent_filesystem_permissions() -> list[Any]:
    """The agent's filesystem rules, as one list, in the order they are matched.

    A module-level function and not an inline literal so the tests can assert on
    the rules that actually ship. A test holding its own copy of this list is a
    test of the copy, and it stays green through any change to the original —
    which is the whole failure mode `check-agent-fs-permissions.sh` exists for.

    ORDER IS THE MECHANISM. `_check_fs_permission` is first-match-wins
    (deepagents/middleware/filesystem.py:111-116), so the allow must sit ahead
    of the deny. Swapped, the deny matches `/skills/authored/**` too, every
    authored write is refused, and nothing reports a misconfiguration — the
    self-improvement loop is simply off.

    The deny itself is the older rule and still the important one: the agent may
    READ its standing orders and may not rewrite them. `FilesystemBackend`
    implements `write` and `edit`, and deepagents allows any operation no rule
    matches, so without it the assistant could rewrite the four bundled SKILL.md
    files on the live container — and text in an ingested web page could in
    principle instruct it to.
    """
    from deepagents import FilesystemPermission

    from aleph_api.authored_skills import AUTHORED_PREFIX

    return [
        FilesystemPermission(operations=["write"], paths=[f"{AUTHORED_PREFIX}**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/skills/**"], mode="deny"),
    ]


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


async def _authorized(project_id: UUID | None) -> UUID | None:
    """Return ``project_id`` only if the request's principal may act on it.

    Fails closed: an unbound principal raises rather than being treated as
    permitted, so an agent tool invoked outside an authenticated request cannot
    reach project data.

    The membership lookup happens **here** rather than being assumed. HTTP routes
    get their role from `project_scope_dep`, a FastAPI dependency keyed on a
    `project_id` *path param* — which `/copilotkit/agent/assistant` does not
    have. So on the agent path nothing ever called `cache_role`, `role_in`
    returned None for everyone, and `require_at_least` denied every tool call:
    the endpoint was authenticated but unusable. Resolving membership on demand
    (and caching it on the principal for the rest of the run) makes the agent
    path authorize on the same fact the HTTP path does, from the same table.
    """
    if project_id is None:
        return None

    principal = current_principal()
    if principal is not None and principal.role_in(project_id) is None:
        from aleph_db.repos.project import get_member

        session_maker = _runtime.get("session_maker")
        if session_maker is None:
            msg = f"cannot verify membership for project {project_id}: no session maker bound"
            raise PermissionDenied(msg)
        async with session_maker() as session:
            member = await get_member(session, project_id=project_id, user_id=principal.user_id)
        # Cache the miss as well as the hit: a non-member must not trigger a
        # fresh query per tool call, and `require_project_access` turns the
        # cached None into the denial.
        principal.cache_role(project_id, member.role if member is not None else None)

    require_project_access(project_id, at_least=ProjectRole.VIEWER)
    return project_id


async def _project_id_from_config(config: RunnableConfig | None) -> UUID | None:
    """Resolve AND AUTHORIZE the project scope for this agent run.

    `ag-ui-langgraph` only threads `thread_id` into `configurable` (it ignores
    request-level config and routes `forwarded_props` elsewhere), so the
    reliable channel is a project-prefixed thread id of the form
    `proj:<uuid>:<thread>`, which the Node CopilotRuntime formats. An explicit
    `projectId`/`project_id` in configurable/metadata is accepted for direct
    callers (curl, tests).

    **Every one of those channels is client-supplied.** This function used to
    return whatever the client named, and its ~8 call sites — the agent's wiki,
    notes, hypotheses, artifact and synthesis tools — then operated on that
    project. Combined with `/copilotkit` sitting on the auth middleware's
    exemption list, that was an unauthenticated read/write primitive against any
    project in the database.

    So the id is now authorized against the request's principal before it is
    returned, here rather than at each call site: a check every caller must
    remember is a check that will eventually be forgotten, and this one was
    forgotten in all eight places at once.
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
        return await _authorized(_project_id_from_thread_id(configurable.get("thread_id")))
    try:
        return await _authorized(UUID(str(raw)))
    except ValueError:
        return None


@tool
async def search_wiki(query: str, config: RunnableConfig, top_k: int = 6) -> str:
    """Quickly SCAN the project's wiki for which pages exist on a topic.

    Returns matching page titles + one-line summaries — a scan to help you
    decide what to retrieve, NOT enough to answer from. To actually ANSWER a
    substantive question, delegate to the `retriever` subagent (it reads the
    page bodies and returns a cited answer); do not compose a substantive answer
    from these summaries alone.
    """
    session_maker = _runtime.get("session_maker")
    project_id = await _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Wiki search is unavailable (no project scope on this run)."
    limit = max(1, min(top_k, 20))
    async with session_maker() as session:  # type: AsyncSession
        svc = IndexService(session)
        hits = await svc.select_pages(project_id=project_id, query=query, top_k=limit)
        fallback = False
        if not hits:
            # Keyword search missed (e.g. a generic/conceptual query). Fall back
            # to the project's actual pages so the agent sees the wiki exists
            # rather than reporting it empty.
            hits = await svc.list_pages(project_id=project_id, top_k=limit)
            fallback = True
    if not hits:
        return "The wiki has no pages yet for this project."
    # The page_id is in every line because `open_page` takes one and the agent
    # had no way to obtain one: this formatter emitted title, kind, score and
    # summary, so `open_page` had no reachable success path at all and the agent
    # could only guess an id or give up. A scan that cannot be acted on is a
    # scan that wastes a turn.
    lines = []
    for h in hits:
        stub = " (stub)" if h.is_stub else ""
        score = "" if fallback else f" · score={h.score:.2f}"
        lines.append(
            f"- [[{h.title}]]{stub} · {h.page_kind}{score}\n"
            f"  page_id={h.page_id} · slug={h.slug}\n"
            f"  {h.summary or '(no summary)'}"
        )
    header = (
        f'No page directly matched "{query}", but the wiki covers these pages:'
        if fallback
        else "Relevant wiki pages:"
    )
    footer = (
        "\n\n(Scan only — these are titles + summaries. To answer a substantive "
        "question, delegate to the `retriever` subagent for a grounded, cited "
        "answer rather than replying from these summaries. To open one in the "
        "workspace, pass its page_id to `open_page`.)"
    )
    return header + "\n" + "\n".join(lines) + footer


@tool
async def wiki_curation_status(config: RunnableConfig) -> str:
    """Report what in the project's wiki needs curation attention.

    Returns page counts by lifecycle status, the titles of drafts awaiting
    review, and the number of unresolved (broken) wikilinks. Call this when the
    analyst asks how the wiki is doing, what needs review, how many pages exist,
    or before proposing curation actions. It is the ONLY way to get exact
    counts — `search_wiki` returns ranked matches and caps at the top hits, so
    counting its results always undercounts.

    The statuses are not interchangeable:

    - `stub` — a title some page linked to that nobody has written yet. It has
      no content and nobody proposed it, so it is NOT awaiting review and must
      never be described as a draft or as work pending approval. Stubs are
      normally most of a grown wiki. They become drafts on their own once two
      separate pages cite them.
    - `draft` — has content and is genuinely waiting for the analyst.
    - `approved` / `archived` — reviewed, and settled.
    """
    from sqlalchemy import func, select

    from aleph_wiki.models import WikiLink, WikiPage

    session_maker = _runtime.get("session_maker")
    project_id = await _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Wiki curation status is unavailable (no project scope on this run)."
    async with session_maker() as session:  # type: AsyncSession
        status_rows = (
            await session.execute(
                select(WikiPage.status, func.count())
                .where(WikiPage.project_id == project_id)
                .group_by(WikiPage.status)
            )
        ).all()
        draft_titles = list(
            (
                await session.execute(
                    select(WikiPage.title)
                    .where(WikiPage.project_id == project_id, WikiPage.status == "draft")
                    .order_by(WikiPage.title)
                    .limit(25)
                )
            )
            .scalars()
            .all()
        )
        broken = (
            await session.execute(
                select(func.count()).where(
                    WikiLink.project_id == project_id, WikiLink.dst_page_id.is_(None)
                )
            )
        ).scalar_one()
        from aleph_wiki.models import PageMergeProposal

        pending_merges = (
            await session.execute(
                select(func.count()).where(
                    PageMergeProposal.project_id == project_id,
                    PageMergeProposal.status == "pending",
                )
            )
        ).scalar_one()
    counts: dict[str, int] = dict(status_rows)
    total = sum(counts.values())
    if total == 0:
        return "The wiki has no pages yet for this project."
    stubs = counts.get("stub", 0)
    awaiting = counts.get("draft", 0)
    parts = [
        f"Wiki curation status — {total} page{'s' if total != 1 else ''}: "
        + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())),
        f"Awaiting your review: {awaiting}."
        + (
            f" The {stubs} stub{'s' if stubs != 1 else ''} are unwritten titles other "
            "pages link to, not work pending approval — do not count them as drafts."
            if stubs
            else ""
        ),
        f"Unresolved (broken) wikilinks: {broken}"
        + (" — these can be fixed with the Repair-links action." if broken else "."),
    ]
    if pending_merges:
        parts.append(
            f"Pending page-merge proposals awaiting approval: {pending_merges} "
            "— review them on the Briefs tab (Approve to merge the duplicate, or Reject)."
        )
    if draft_titles:
        parts.append("Drafts awaiting review:\n" + "\n".join(f"- [[{t}]]" for t in draft_titles))
    return "\n".join(parts)


@tool
async def wiki_schema(config: RunnableConfig) -> str:
    """Report the wiki's governance schema: domain, categories, tag taxonomy, thresholds.

    Read this BEFORE writing or filing any wiki page. It is the controlled
    vocabulary the write path validates against — a page carrying a tag or a
    category that is not listed here is rejected at commit time, not fixed
    later. It also states the page thresholds: how many links a page needs, how
    long before it should be split, and how many citations an unwritten title
    needs before it earns a page.

    Call this when the analyst asks what the wiki covers, what the categories or
    tags are, how pages are organised, or before proposing any change to how
    pages are filed.
    """
    session_maker = _runtime.get("session_maker")
    project_id = await _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "The wiki schema is unavailable (no project scope on this run)."
    async with session_maker() as session:  # type: AsyncSession
        svc = SchemaService(session)
        schema = await svc.get(project_id)
        customised = await svc.is_customised(project_id)

    lines = [
        f"Wiki domain: {schema.domain}",
        "",
        (
            "This schema was derived for this project."
            if customised
            else "This project is still on the SHIPPED DEFAULT schema, which describes "
            "AI/ML research. If the corpus is about something else, the categories "
            "below are the wrong ones — say so, and offer to derive a schema from "
            "the pages that actually exist."
        ),
        "",
        "Categories (file every page under exactly one, by id):",
    ]
    lines += [
        f"  - {c.id}: {c.title}" + (f" — {c.blurb}" if c.blurb else "") for c in schema.categories
    ]
    lines += [
        "",
        f"Page types: {', '.join(schema.page_types)}",
        "",
        "Tag taxonomy — use ONLY these, 2-5 per page. A tag not on this list is "
        "rejected at write time; to use a new one, add it to the schema first:",
        "  " + ", ".join(schema.tags),
        "",
        "Thresholds:",
        f"  - every page needs at least {schema.min_outbound_links} outbound [[wikilinks]]",
        f"  - split a page past {schema.page_split_lines} lines",
        f"  - an unwritten title earns a page at {schema.stub_promotion_mentions} "
        "citing pages; promotion moves it to the WRITING queue (planned), never "
        "to the review queue (draft)",
    ]
    return "\n".join(lines)


@tool
async def wiki_lint_report(
    severity: str = "",
    limit: int = 30,
    config: RunnableConfig | None = None,
) -> str:
    """Run the wiki health check and report what is wrong, worst first.

    Checks broken wikilinks, orphan pages, uncategorised pages, schema
    violations, frontmatter that disagrees with the stored fields, contested
    pages, unjudged confidence, staleness, tags outside the taxonomy, duplicate
    slugs, near-duplicate pages that should be merged, and unwritten titles that
    have earned a page.

    Call this when the analyst asks to lint, audit or health-check the wiki, asks
    what is broken or missing, or asks what to work on next.

    Args:
        severity: optional filter — "broken", "structure", "quality", "style",
            or a comma-separated set. Omit for everything.
        limit: how many findings to include. Default 30.
    """
    session_maker = _runtime.get("session_maker")
    project_id = await _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "The wiki lint is unavailable (no project scope on this run)."
    async with session_maker() as session:  # type: AsyncSession
        schema = await SchemaService(session).get(project_id)
        report = await lint_wiki(session, project_id=project_id, schema=schema)

    if severity:
        wanted = {s.strip() for s in severity.split(",") if s.strip()}
        kept = [f for f in report.sorted_findings() if f.severity in wanted]
        if not kept:
            return f"Wiki lint — {report.pages_scanned} pages checked: no {severity} findings."
        head = (
            f"Wiki lint — {report.pages_scanned} pages checked "
            f"({report.stubs_skipped} stubs skipped): {len(kept)} {severity} findings."
        )
        body = "\n".join(
            f"  - [{f.check}] {f.message}"
            + (f" ({f.page_title})" if f.page_title else "")
            + (f" — {f.fix}" if f.fix else "")
            for f in kept[: max(1, limit)]
        )
        return f"{head}\n{body}"

    summary = report.summary()
    # `summary()` caps at 60 lines; honour a smaller ask so a request for the
    # top few does not return three screens.
    if limit < 60:
        head, _, rest = summary.partition("\n")
        kept_lines = [ln for ln in rest.splitlines() if ln.strip()][: max(1, limit) + 6]
        return head + "\n" + "\n".join(kept_lines)
    return summary


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

    from aleph_api.chat_runs import run_id_from_config
    from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter
    from aleph_db.models.model_profile import ModelProfile

    session_maker = _runtime.get("session_maker")
    litellm = _runtime.get("litellm")
    settings = _runtime.get("settings")
    project_id = await _project_id_from_config(config)
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
        # The turn this search belongs to. Not `None`: the router makes three
        # further model calls of its own (`corpus_search.query_embed`,
        # `page_selection`, `compose`), and passing None writes all three as
        # unattributed — priced, but belonging to nothing. The live probe found
        # exactly that: 13 attributed `assistant.turn` rows alongside 9
        # orphans, all from inside this one tool.
        agent_run_id=run_id_from_config(config),
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
    project_id = await _project_id_from_config(config)
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
    project_id = await _project_id_from_config(config)
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
    project_id = await _project_id_from_config(config)
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
    route (connector resolution + the native research job dispatch; rule #3
    — never touches the DB directly) and returns immediately. Reused by the
    `researcher` subagent's `start_research` tool (Wave 3 T3) so the orchestrator
    delegates research rather than self-calling it inline.

    `depth` is "shallow" (fast, single pass, ~1 min) or "deep" (thorough,
    multi-loop, several minutes); defaults to "shallow" for responsiveness.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = await _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Research is unavailable (no project scope on this run)."
    depth = depth if depth in ("shallow", "deep") else "shallow"
    # Self-call the synthesize endpoint so we reuse the full, tested dispatch
    # path (connector resolution + the native deep_research_job dispatch).
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/synthesize",
                json={"topic": query, "depth": depth},
                headers=await _self_headers(project_id, settings=settings),
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
    project_id = await _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot pin (no project scope)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/cards/pin",
                json={"card_kind": card_kind, "title": title, "props": props},
                headers=await _self_headers(project_id, settings=settings),
            )
    except Exception as exc:
        return f"Could not pin the {card_kind}: {exc}"
    if resp.status_code >= 400:
        return f"Could not pin the {card_kind} ({resp.status_code}): {resp.text[:200]}"
    return f"Pinned '{title}' to the Briefs tab (card {resp.json()['card_id']})."


async def _dispatch_card_action_impl(
    action_kind: str,
    params: dict[str, Any],
    config: RunnableConfig,
) -> dict[str, Any]:
    """Self-call the tested `/cards/actions` route (rule #3 — never raw DB).

    The route runs the action through the ONE ledger-audited ActionRouter, so
    the agent's composition verbs (compose_dossier / spotlight) are audited
    identically to an analyst's card click (CardAction row + ledger event).
    Returns the route's `{action_id, ok, result}` body, or `{"error": ...}`.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = await _project_id_from_config(config)
    if settings is None or project_id is None:
        return {"error": "no project scope on this run"}
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/cards/actions",
                json={
                    "surface_kind": "ChatSurface",
                    "action_kind": action_kind,
                    "params": params,
                },
                headers=await _self_headers(project_id, settings=settings),
            )
    except Exception as exc:
        return {"error": f"{action_kind} dispatch failed: {exc}"}
    if resp.status_code >= 400:
        return {"error": f"{action_kind} rejected ({resp.status_code}): {resp.text[:200]}"}
    return resp.json()


@tool
async def pin_to_brief(
    card_kind: str,
    title: str,
    props: dict[str, Any],
    config: RunnableConfig,
) -> str:
    """Pin a catalog card to the Briefs tab so it survives the chat transcript.

    `card_kind` is a catalog component name (e.g. "ClaimCard", "TableCard",
    "WikiPageCard"); `title` labels the pinned card; `props` are the card's
    props matching that component's schema. Use this to keep a card the analyst
    should be able to return to (a key claim, a comparison table, a composed
    view). It renders in the Briefs tab until the analyst unpins it.
    """
    return await _pin_to_briefs_impl(card_kind, title, props, config)


@tool
async def compose_dossier(
    title: str,
    config: RunnableConfig,
    card_ids: list[str] | None = None,
    page_ids: list[str] | None = None,
) -> str:
    """Compose a derived, read-only dossier card grouping wiki pages and/or cards.

    Creates a single read-only Briefs card titled `title` that groups the
    referenced wiki pages (`page_ids`, linked as [[wikilinks]]) and pinned cards
    (`card_ids`). Use it to assemble a themed collection the analyst can open
    from Briefs. The dossier is persisted and audited through the action router.
    """
    result = await _dispatch_card_action_impl(
        "compose_dossier",
        {
            "title": title,
            "card_ids": list(card_ids or []),
            "page_ids": list(page_ids or []),
        },
        config,
    )
    if "error" in result:
        return f"Could not compose the dossier: {result['error']}"
    data = result.get("result", {})
    return (
        f"Composed dossier '{title}' (card {data.get('card_id')}) grouping "
        f"{data.get('page_count', 0)} page(s) and {data.get('card_count', 0)} "
        "card(s). It's pinned to the Briefs tab — open Briefs to see it."
    )


@tool
async def spotlight(card_id: str, config: RunnableConfig) -> str:
    """Spotlight a Briefs card so it sorts to the top of the pile.

    `card_id` is the UUID of a pinned/composed Briefs card. Spotlighting marks it
    so the Briefs surface orders it first and flags it visually. Use it to draw
    the analyst's attention to the card that matters most right now. Audited
    through the action router.
    """
    try:
        UUID(card_id)
    except ValueError:
        return f"card_id must be a valid UUID (got '{card_id}')."
    result = await _dispatch_card_action_impl("spotlight", {"card_id": card_id}, config)
    if "error" in result:
        return f"Could not spotlight the card: {result['error']}"
    return f"Spotlighted card {card_id} — it now sorts to the top of the Briefs tab."


async def _render_code_via_runner_impl(
    code: str,
    output_kind: str,
    config: RunnableConfig,
    title: str = "Sandbox chart",
    pin: bool = True,
) -> str:
    """Run agent-written Python in the isolated sandbox → versioned artifact → pin.

    Self-calls the tested `/viz/render-code` route (rule #3 — never touches the
    DB or executes code here). The API dispatches to `aleph-workers`, which fans
    the code out to the network-less `aleph-code-runner`, awaits the bytes,
    persists a versioned artifact (checksum + producing_code + lineage), and pins
    the resulting card to Briefs. The agent's process never runs the code
    (amended rule 8)."""
    import httpx

    settings = _runtime.get("settings")
    project_id = await _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot render code (no project scope)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/viz/render-code",
                json={
                    "code": code,
                    "output_kind": output_kind,
                    "title": title,
                    "pin": pin,
                },
                headers=await _self_headers(project_id, settings=settings),
            )
    except Exception as exc:
        return f"Could not dispatch the sandbox render: {exc}"
    if resp.status_code >= 400:
        return f"Sandbox render could not start ({resp.status_code}): {resp.text[:200]}"
    body = resp.json()
    if not body.get("dispatched"):
        return (
            f"Queued the {output_kind} render of '{title}', but the sandbox "
            "did not accept the dispatch — the code_runner may be unavailable."
        )
    return (
        f"Rendering '{title}' ({output_kind}) in the sandbox. It runs isolated "
        "(no network, no credentials); when it finishes the chart is a versioned "
        "artifact pinned to the Briefs tab. Open Briefs to see it."
    )


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
    project_id = await _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot ingest (no project scope)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base}/v1/projects/{project_id}/sources/ingest-url",
                json={"url": url, "title": title},
                headers=await _self_headers(project_id, settings=settings),
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
                headers=await _self_headers(project_id, settings=settings),
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
    project_id = await _project_id_from_config(config)
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
    project_id = await _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Connectors are unavailable (no project scope on this run)."
    base = settings.aleph_self_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            connectors_resp = await client.get(
                f"{base}/v1/connectors",
                headers=await _self_headers(project_id, settings=settings),
            )
            bindings_resp = await client.get(
                f"{base}/v1/projects/{project_id}/connectors/bindings",
                headers=await _self_headers(project_id, settings=settings),
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
    project_id = await _project_id_from_config(config)
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
    """Switch the project's model profile to a named template, or report it.

    `profile_name` is one of "aleph-dev" (Sonnet/Haiku) or "aleph-production"
    (Opus/Sonnet). Pass a name to switch; the project's per-capability bindings
    are replaced with that template's. If the embedding model changes, the
    project's chunks are re-embedded in the background. Pass an empty string to
    just report the current + available profiles.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = await _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Model profile is unavailable (no project scope on this run)."
    base = settings.aleph_self_url
    name = (profile_name or "").strip()
    async with httpx.AsyncClient(timeout=30.0) as client:
        if name:
            try:
                resp = await client.post(
                    f"{base}/v1/projects/{project_id}/model-profile/switch",
                    headers=await _self_headers(project_id, settings=settings),
                    json={"profile_name": name},
                )
            except Exception as exc:
                return f"Could not switch the model profile: {exc}"
            if resp.status_code >= 400:
                return f"Could not switch to '{name}' ({resp.status_code}): {resp.text[:200]}"
            return (
                f"Switched the project's model profile to '{name}'. New LLM/agent "
                "calls use that profile's models; if the embedding model changed, "
                "the project's sources are re-embedding in the background."
            )
        try:
            current_resp = await client.get(
                f"{base}/v1/projects/{project_id}/model-profile",
                headers=await _self_headers(project_id, settings=settings),
            )
            templates_resp = await client.get(
                f"{base}/v1/model-profile-templates",
                headers=await _self_headers(project_id, settings=settings),
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
        f"Available profiles: {', '.join(available)}. Pass a name to switch."
    )


@tool
async def diagnose_platform(config: RunnableConfig, window_hours: int = 24) -> str:
    """Inspect the platform's own Langfuse traces to report what is happening
    and what is broken.

    Reads back the OTEL traces the platform emits (LLM calls, tool calls,
    research/wiki jobs, HTTP) over the last `window_hours` and summarizes trace
    volume plus the most recent ERROR-level observations (failed LLM calls,
    tool errors, exceptions) with their status messages. Call this when the
    analyst asks how the system is doing, why something failed or stalled, or
    whether recent errors are occurring. Read-only — it never changes anything.
    """
    from aleph_observability import LangfuseReader

    settings = _runtime.get("settings")
    if settings is None:
        return "Platform diagnostics are unavailable (no runtime settings on this run)."
    host = getattr(settings, "langfuse_host", "")
    public_key = getattr(settings, "langfuse_public_key", "")
    secret_key = getattr(settings, "langfuse_secret_key", "")
    if not (host and public_key and secret_key):
        return "Platform diagnostics are unavailable (Langfuse is not configured)."
    # From inside compose the app talks to `langfuse:3000`; keep that as-is.
    reader = LangfuseReader(host=host, public_key=public_key, secret_key=secret_key)
    try:
        snap = await reader.diagnostic_snapshot(window_hours=window_hours)
    except Exception as exc:
        return f"Could not read platform traces from Langfuse: {exc}"
    finally:
        await reader.aclose()

    header = (
        f"Platform health over the last {snap.window_hours}h: "
        f"{snap.total_traces} traces, {snap.total_observations} observations, "
        f"{snap.error_count} error{'s' if snap.error_count != 1 else ''}."
    )
    if not snap.recent_errors:
        return (
            header
            + " No ERROR-level observations in this window — nothing broken is being recorded."
        )
    lines = [header, "Most recent errors:"]
    for err in snap.recent_errors:
        msg = err.status_message or "(no status message)"
        when = err.start_time[:19] if err.start_time else "?"
        lines.append(f"- {when} · {err.name}: {msg} (trace {err.trace_id})")
    return "\n".join(lines)


def _psycopg_conn_string(database_url: str) -> str:
    """Convert the app's SQLAlchemy asyncpg URL to a psycopg conn string.

    `AsyncPostgresStore` connects with psycopg (not asyncpg), so strip the
    SQLAlchemy `+asyncpg` driver suffix: `postgresql+asyncpg://…` → `postgresql://…`.
    """
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def build_agent_store(
    *, database_url: str, settings: Settings | None = None
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
    # `max_size` is the fix, and it is not a tuning knob. `AsyncConnectionPool`
    # defaults `max_size` to `min_size`, so this pool held exactly ONE
    # connection: every saved checkpoint, every memory read and every one of six
    # concurrent subagents queued behind it and gave up after 30 seconds. That
    # is the shape of "the assistant is slow and then fails" with no error
    # message anywhere.
    from aleph_api.settings import get_settings

    cfg = settings if settings is not None else get_settings()
    pool = cast(
        "AsyncConnectionPool[AsyncConnection[DictRow]]",
        AsyncConnectionPool(
            _psycopg_conn_string(database_url),
            open=False,
            min_size=cfg.aleph_agent_pool_min_size,
            max_size=cfg.aleph_agent_pool_max_size,
            timeout=cfg.aleph_agent_pool_timeout_s,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        ),
    )
    store = AsyncPostgresStore(conn=pool)
    return pool, store


def build_agent_checkpointer(pool: Any) -> Any:
    """Durable per-thread conversation state for the AG-UI assistant.

    Shares the store's already-open pool: `AsyncPostgresSaver` wants exactly the
    connection configuration `build_agent_store` sets up (autocommit, no
    prepared statements, dict rows), so there is no reason to open a second one.

    This replaces an in-memory `MemorySaver`, which meant every API restart —
    and every additional API replica — silently lost the agent's conversation
    history, its `write_todos` plan (the Activity card's plan display), and the
    summarization archive. `test_agent_checkpointer.py` keeps it from
    regressing.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return AsyncPostgresSaver(conn=pool)


# Fallback agent model id, used only when no ModelProfile bindings are bound to
# the runtime (e.g. the named template row is missing). Normally the model is
# resolved per capability from the default profile's bindings (rule #7).
#
# Environment-driven because the fallback has to name a model the configured
# gateway actually serves. A hardcoded default is wrong for any deployment
# pointed at a gateway that does not carry it, and the failure it produces —
# a 404 from the fallback path only — is the kind that shows up long after boot.
_AGENT_MODEL = os.environ.get("ALEPH_FALLBACK_AGENT_MODEL", "claude-sonnet-4-6")


def _resolve_agent_model(capability: Any) -> str:
    """Resolve capability → model from the runtime-bound default profile bindings.

    Honors rule #7: the conversational surface uses the project's selected
    ``ModelProfile`` (the default named profile, loaded at lifespan) instead of a
    hardcoded id, so ``aleph-production`` actually applies Opus to the agent.
    Falls back to ``_AGENT_MODEL`` when no bindings are bound or the capability
    is unmapped.
    """
    from aleph_models.profile import resolve_binding

    bindings = _runtime.get("agent_bindings")
    if not bindings:
        return _AGENT_MODEL
    try:
        return resolve_binding(bindings, capability).model
    except Exception:
        return _AGENT_MODEL


# SKILL.md skills live in `skills/<name>/SKILL.md` alongside this module. The
# Deep Agent reads them through its backend, so a FilesystemBackend rooted here
# is routed under the in-backend `/skills/` prefix (see `_memory_backend`); the
# orchestrator is given `skills=["/skills"]` to scan that source for skills.
_SKILLS_DIR = pathlib.Path(__file__).parent / "skills"


def _openai_base_url(base_url: str) -> str:
    """Normalise the gateway URL for the OpenAI SDK, which wants the `/v1` segment.

    One env var (`LITELLM_BASE_URL`) feeds two clients with opposite conventions:
    `LiteLLMClient` builds `{base}/v1/chat/completions` itself, so it must be
    given a bare origin; `ChatOpenAI` (openai-python) appends only
    `/chat/completions`, so its base must already carry `/v1`. Passing the same
    raw value to both sent agent traffic to `{base}/chat/completions`, which a
    strict OpenAI-compatible server answers with 404 — surfacing in the UI as
    "Run ended without emitting a terminal event", nowhere near the cause.

    Idempotent, so a `LITELLM_BASE_URL` that already ends in `/v1` still works.
    """
    trimmed = base_url.rstrip("/")
    return trimmed if trimmed.endswith("/v1") else f"{trimmed}/v1"


def _gateway_chat_model(settings: Settings, *, purpose: str, capability: Any = None) -> ChatOpenAI:
    """Build a gateway-pointed `ChatOpenAI` with cost attribution (rules #2, #5, #7).

    All agent LLM traffic (orchestrator + every subagent) is constructed here so
    it is configured identically — gateway `base_url`/`api_key`, temperature, and
    `stream_usage=True` — and so each gets its own `AgentCostCallbackHandler`
    tagged with a `purpose`. The model is resolved from the default profile's
    bindings for `capability` (rule #7), falling back to `_AGENT_MODEL`. The
    callback is attached ONLY to the agent model (never to `LiteLLMClient`), so
    the LiteLLMClient retrieval path is not double-counted; it writes a
    `ModelCall` + `CostLedgerEvent` per call (rule #5) and never crashes the turn.
    """
    from aleph_api.copilot_cost_callback import AgentCostCallbackHandler
    from aleph_core.schemas.model_profile import Capability
    from aleph_models.limiter import shared_gateway_client

    model = _resolve_agent_model(capability or Capability.SYNTHESIS)
    base_url = _openai_base_url(settings.litellm_base_url)
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=settings.insights_litellm_api_key,
        temperature=0.2,
        # The agent's traffic goes through the same metered door as everything
        # else. WS-MEP-2 built the limiter and left this seam unwired, so the
        # LARGEST source of concurrent gateway load was the one thing not
        # bounded by it: one orchestrator plus six subagents, each issuing tool
        # calls in parallel, against a ceiling that applied to nobody.
        #
        # `ChatOpenAI` builds its own HTTP client, which is why the limiter has
        # to arrive as a client rather than sit inside Aleph's transport code.
        # `shared_gateway_client` is shared PER ENDPOINT, so all seven models
        # share one pool and one ceiling — seven private unbounded pools is the
        # shape WS-MEP-4 warns about.
        http_async_client=shared_gateway_client(
            base_url, timeout=settings.aleph_agent_request_timeout_s
        ),
        # Configuration, not literals. 60s was below the p99 of a tool-heavy
        # turn against a shared gateway.
        timeout=settings.aleph_agent_request_timeout_s,
        # The SDK's own retry is OFF on purpose. `AlephAgentMiddleware.
        # awrap_model_call` retries with real backoff and honours Retry-After;
        # leaving max_retries here would stack two retry budgets, so being rate
        # limited would multiply the request rate rather than reduce it —
        # exactly backwards, and exactly when the gateway can least afford it.
        max_retries=0,
        callbacks=[AgentCostCallbackHandler(model=model, purpose=purpose)],
        # A streaming OpenAI-compatible response omits the `usage` block unless
        # `stream_options.include_usage` is set. Without this the on_llm_end
        # AIMessage has `usage_metadata=None` and the cost callback has nothing
        # to record (rule #5 gap). `stream_usage=True` makes ChatOpenAI request +
        # aggregate the usage into the final chunk.
        stream_usage=True,
    )


def subagent_model(settings: Settings, name: str, *, capability: Any = None) -> ChatOpenAI:
    """Build a subagent's gateway `ChatOpenAI`, cost-tagged per subagent.

    Identical to the orchestrator's model but the cost callback's `purpose` is
    `assistant.subagent.<name>`, so each subagent's LLM calls write a
    `ModelCall` + `CostLedgerEvent` attributed to that subagent (rule #5). The
    model is resolved for `capability` from the default profile (rule #7).
    """
    return _gateway_chat_model(
        settings, purpose=f"assistant.subagent.{name}", capability=capability
    )


#: The orchestrator's tools, as a module-level list rather than a literal inside
#: the builder. A test can then enumerate what the agent ACTUALLY carries and
#: assert a property of every one of them — which is the half that silently
#: stops being true when somebody adds the twelfth tool.
#: (The UI-driving `open_page` / `focus_tab` / `highlight_claim` are CopilotKit
#: frontend tools and live in the browser, not here.)

# ---------------------------------------------------------------------------
# The kernel, reachable by the agent. WS-A2.
# ---------------------------------------------------------------------------
#
# "An agent that authors plugins for itself and activates or deactivates them as
# needed… The kernel is the product." — CLAUDE.md, first substantive line.
#
# The kernel was built, guarded and tested, and until these tools
# `grep -rn "AgentPluginAPI" apps/api/src` returned 0. The product was a library
# whose only non-test importer was an acceptance probe.
#
# Every one of these resolves scope through `_authorized`, so they inherit the
# agent-scope defence already in place rather than inventing a second one.


#: Fallback actor when a tool runs with no bound principal — a direct caller or
#: a test. Never reached on the chat path, where  has already
#: failed closed on an unbound principal.
_SYSTEM_ACTOR = UUID(int=0)


def _agent_plugin_api() -> Any:
    from aleph_kernel.agent_api import AgentPluginAPI

    kernel = _runtime.get("kernel")
    if kernel is None:
        msg = "the kernel is not mounted on this process"
        raise RuntimeError(msg)
    return AgentPluginAPI(kernel)


@tool
async def list_capabilities(config: RunnableConfig) -> str:
    """List every capability this system has, and whether you may turn it off.

    Use this before proposing any change to the system's own abilities. A
    capability with `plugin_id: null` is core: it is not merely protected, it
    has no handle you could pass anywhere, so there is nothing to attempt.
    """
    await _authorized(await _project_id_from_config(config))
    views = _agent_plugin_api().inspect()
    if not views:
        return "No capabilities are mounted on this process."
    lines = []
    for v in views:
        handle = v.plugin_id or "core (no handle — cannot be addressed)"
        lines.append(
            f"- {v.name} [{v.state}] provides={list(v.provides)} "
            f"requires={list(v.requires)} removable={v.removable} handle={handle}"
        )
    return "\n".join(lines)


@tool
async def preview_removal(plugin_id: str, config: RunnableConfig) -> str:
    """Say what ELSE would stop if this plugin were turned off. Changes nothing.

    Always call this before `disable_plugin`. The blast radius is computed from
    the declaration graph, so the answer here is exactly the refusal you would
    get — a refusal you could not have predicted is indistinguishable from a
    broken tool.
    """
    await _authorized(await _project_id_from_config(config))
    for v in _agent_plugin_api().inspect():
        if v.plugin_id == plugin_id:
            also = list(getattr(v, "would_also_stop", ()) or ())
            if not also:
                return f"Disabling {v.name} would stop nothing else."
            return (
                f"Disabling {v.name} would ALSO stop: {', '.join(sorted(also))}. "
                "Disabling it will be refused unless you accept breaking those."
            )
    return (
        f"No addressable plugin {plugin_id!r}. Core capability is mounted from the "
        "boot manifest and has no plugin id — it cannot be named here at all."
    )


@tool
async def author_plugin(
    name: str,
    instructions: str,
    config: RunnableConfig,
    code: str = "",
    requires: list[str] | None = None,
) -> str:
    """Write a new plugin for yourself, durably.

    `instructions` is a SKILL.md document with `---` front matter carrying at
    least `name` and `description`. `code` is optional Python defining helpers;
    it is checked before it is stored and before it ever runs — a top-level call
    outside a small allowlist is refused, so importing your plugin cannot have a
    side effect.

    The plugin is recorded in the database, so it is still there after a restart
    and the background workers can load it too.
    """
    project_id = await _authorized(await _project_id_from_config(config))
    if project_id is None:
        return "No project in scope; cannot install a plugin."

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_kernel.skills import SkillRejected
    from aleph_runtime.plugin_service import PluginDraft, PluginService

    session_maker = _runtime.get("session_maker")
    if session_maker is None:
        return "No database bound on this process; cannot install a plugin durably."
    principal = current_principal()
    try:
        async with session_maker() as session:
            row = await PluginService(session).install(
                project_id=project_id,
                actor_id=principal.user_id if principal else _SYSTEM_ACTOR,
                draft=PluginDraft(
                    name=name,
                    instructions=instructions,
                    code=code,
                    requires=tuple(requires or ()),
                ),
                ledger=LedgerWriter(session),
                kernel=_runtime.get("kernel"),
            )
            await session.commit()
    except SkillRejected as exc:
        # The violations, verbatim. An agent told only "rejected" writes the
        # same plugin again.
        return f"Refused: {exc}"
    return (
        f"Installed {row.name!r} (v{row.major_version}). It will still be here "
        "after a restart, and the workers can load it too."
    )


@tool
async def disable_plugin(plugin_id: str, config: RunnableConfig, force: bool = False) -> str:
    """Turn off a plugin you installed. Refused if something depends on it.

    Call `preview_removal` first. `force` accepts breaking the plugins named
    there; it can never reach core capability, because core capability has no
    id to pass here.
    """
    project_id = await _authorized(await _project_id_from_config(config))
    if project_id is None:
        return "No project in scope."

    from uuid import UUID as _UUID

    from aleph_kernel.kernel import PluginId

    api = _agent_plugin_api()
    known = {v.plugin_id: v.name for v in api.inspect() if v.plugin_id}
    if plugin_id not in known:
        return (
            f"No addressable plugin {plugin_id!r}. Core capability has no plugin "
            "id and cannot be disabled."
        )
    outcome = await api.disable(PluginId(_UUID(plugin_id)), force=force)
    # `installed=True` means REFUSED: the plugin is still installed. The field
    # answers "is it installed", not "did the call succeed".
    if outcome.installed:
        return f"Refused: {outcome.detail}"

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_runtime.plugin_service import PluginService

    session_maker = _runtime.get("session_maker")
    principal = current_principal()
    if session_maker is None:
        # The kernel already dropped it; the row just cannot be updated here.
        return f"Disabled {known[plugin_id]!r} in this process (no database bound)."
    async with session_maker() as session:
        await PluginService(session).disable(
            project_id=project_id,
            actor_id=principal.user_id if principal else _SYSTEM_ACTOR,
            name=known[plugin_id],
            ledger=LedgerWriter(session),
        )
        await session.commit()
    return f"Disabled {known[plugin_id]!r}. The record is kept, so it can be turned back on."


@tool
async def plugin_health(config: RunnableConfig) -> str:
    """Re-run every capability's own probe and report what answered.

    A capability that cannot answer a live query is reported here rather than
    discovered by the next thing that needs it.
    """
    await _authorized(await _project_id_from_config(config))
    health = await _agent_plugin_api().check_health()
    if not health:
        return "No capabilities to probe."
    return "\n".join(f"- {name}: {state}" for name, state in sorted(health.items()))


# ---------------------------------------------------------------------------
# WS-H6: long jobs return a ticket
# ---------------------------------------------------------------------------
#
# The routes, the ticket record, the worker job and the cancellation machinery
# all shipped first, and nothing could reach any of it: `grep -rn
# "background-tasks"` found the route file, its test, and one docstring. A
# reindex or a review sweep started inline still blocks the turn for minutes,
# which is the entire problem the workstream exists to solve.
#
# These three tools are that consumer. They self-call over HTTP with the run's
# own agent token rather than touching the database, which is the same path
# `start_research` takes and the same rule the workers follow: agents never
# write state directly.


@tool
async def start_background_task(kind: str, config: RunnableConfig) -> str:
    """Start a long job and get a ticket back immediately, instead of waiting.

    Use this for work measured in minutes rather than seconds — reindexing the
    corpus, sweeping pages for review. You get a ticket id straight away and the
    conversation continues; call `check_background_task` with that id to see
    progress, and `cancel_background_task` to stop it.

    Call this with no `kind` you have not seen listed: the error names the kinds
    that exist.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = await _authorized(await _project_id_from_config(config))
    if settings is None or project_id is None:
        return "Background work is unavailable (no project scope on this run)."
    # The parent link, so the Inspector can show the hand-off on the turn that
    # made it rather than as an orphan run.
    from aleph_api.chat_runs import run_id_from_config

    parent = run_id_from_config(dict(config)) if config else None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.aleph_self_url}/v1/projects/{project_id}/background-tasks",
                json={
                    "kind": kind,
                    "params": {},
                    "parent_agent_run_id": str(parent) if parent else None,
                },
                headers=await _self_headers(project_id, settings=settings),
            )
    except Exception as exc:
        return f"Could not start that job: {exc}"
    if resp.status_code >= 400:
        return f"Could not start {kind!r} ({resp.status_code}): {resp.text[:300]}"
    body = resp.json()
    return (
        f"Started {body['kind']} in the background. Ticket "
        f"{body['agent_run_id']} — it is {body['status']} now. Ask me to check "
        "on it whenever you like; I will not block on it."
    )


@tool
async def check_background_task(ticket_id: str, config: RunnableConfig) -> str:
    """Report a background job's real progress: phase, units done, status.

    The numbers come from the job's own recorded events, not from an estimate.
    A ticket that has not reported a phase yet has genuinely not started one.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = await _authorized(await _project_id_from_config(config))
    if settings is None or project_id is None:
        return "Background work is unavailable (no project scope on this run)."
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{settings.aleph_self_url}/v1/projects/{project_id}/background-tasks/{ticket_id}",
                headers=await _self_headers(project_id, settings=settings),
            )
    except Exception as exc:
        return f"Could not check that ticket: {exc}"
    if resp.status_code == 404:
        return f"No background job with ticket {ticket_id} in this project."
    if resp.status_code >= 400:
        return f"Could not check {ticket_id} ({resp.status_code}): {resp.text[:300]}"
    body = resp.json()
    phase = body.get("phase") or "not started"
    done = body.get("units_done")
    total = body.get("units_total")
    progress = f", {done}/{total}" if done is not None and total else ""
    error = f" — {body['error_text']}" if body.get("error_text") else ""
    return f"{body['kind']} is {body['status']} (phase: {phase}{progress}){error}"


@tool
async def cancel_background_task(ticket_id: str, config: RunnableConfig) -> str:
    """Stop a running background job. It stops at its next checkpoint.

    Cancelling is a real stop, not a relabel: the handler checks between units
    of work, so a sweep that has done 40 of 200 pages does not go on to do the
    other 160.
    """
    import httpx

    settings = _runtime.get("settings")
    project_id = await _authorized(await _project_id_from_config(config))
    if settings is None or project_id is None:
        return "Background work is unavailable (no project scope on this run)."
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.aleph_self_url}/v1/projects/{project_id}"
                f"/background-tasks/{ticket_id}/cancel",
                headers=await _self_headers(project_id, settings=settings),
            )
    except Exception as exc:
        return f"Could not cancel that ticket: {exc}"
    if resp.status_code == 404:
        return f"No background job with ticket {ticket_id} in this project."
    if resp.status_code >= 400:
        return f"Could not cancel {ticket_id} ({resp.status_code}): {resp.text[:300]}"
    body = resp.json()
    if not body.get("cancelled"):
        return f"Ticket {ticket_id} was already {body.get('status', 'finished')}."
    return f"Cancelling {ticket_id}. It stops at its next checkpoint."


_ORCHESTRATOR_TOOLS: tuple[Any, ...] = (
    search_wiki,
    wiki_curation_status,
    wiki_schema,
    wiki_lint_report,
    list_connectors,
    set_connector_enabled,
    set_model_profile,
    diagnose_platform,
    pin_to_brief,
    compose_dossier,
    spotlight,
    # WS-H6: long jobs return a ticket instead of blocking the turn.
    start_background_task,
    check_background_task,
    cancel_background_task,
    # WS-A2: the kernel, reachable.
    list_capabilities,
    preview_removal,
    author_plugin,
    disable_plugin,
    plugin_health,
)


def build_assistant_deep_agent(
    *, settings: Settings, store: AsyncPostgresStore, checkpointer: Any = None
):
    """Compile the assistant Deep Agent (built once at app startup).

    `checkpointer` holds per-thread conversation state. Production passes the
    Postgres-backed `build_agent_checkpointer(...)`; it is optional only so
    tests can compile the graph without a database, and falls back to an
    in-memory saver in that case. Passing nothing in production silently
    reverts to losing every conversation on restart — see
    `build_agent_checkpointer`.

    Returns a LangGraph `CompiledStateGraph` suitable for
    `LangGraphAGUIAgent(graph=...)`. W3 extends this with subagents.

    `store` is the long-lived Postgres-backed langgraph store created by the
    lifespan; a `CompositeBackend` routes `/memories/` to it (cross-session
    persistence) while all other agent files stay ephemeral per-thread.
    """
    from copilotkit import CopilotKitMiddleware
    from deepagents import create_deep_agent
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

    from aleph_api.agent_middleware import AlephAgentMiddleware
    from aleph_api.authored_skills import (
        SKILL_SOURCES,
        AuthoredSkillsMiddleware,
        authored_namespace,
    )
    from aleph_api.subagents.analyst import build_analyst_subagent
    from aleph_api.subagents.researcher import build_researcher_subagent
    from aleph_api.subagents.retriever import build_retriever_subagent
    from aleph_api.subagents.reviewer import build_reviewer_subagent
    from aleph_api.subagents.viz_builder import build_viz_builder_subagent
    from aleph_api.subagents.wiki_builder import build_wiki_builder_subagent
    from aleph_db.repos.agent_runs import SYSTEM_ACTOR

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
                # WS-H1: the one writable place a skill can go. Nested INSIDE
                # `/skills/` on purpose — CompositeBackend sorts routes
                # longest-prefix-first (backends/composite.py:162-163), so this
                # wins for its own prefix while everything else under
                # `/skills/` still resolves to the read-only bundled set.
                "/skills/authored/": StoreBackend(namespace=authored_namespace),
                "/skills/": _skills_backend,
            },
        )

    # The orchestrator's OWN model. Cost is attributed to `assistant.turn` via
    # the AgentCostCallbackHandler that `_gateway_chat_model` attaches (rule #5).
    model = _gateway_chat_model(settings, purpose="assistant.turn")

    # In-memory checkpointer keeps per-thread conversation state for the AG-UI
    # runtime. Cross-SESSION durability rides the `store` instead: the
    # CompositeBackend routes `/memories/` to a StoreBackend over the
    # Postgres-backed langgraph store, so memory files survive new threads and
    # process restarts. Everything else stays ephemeral (StateBackend).
    return create_deep_agent(
        model=model,
        tools=list(_ORCHESTRATOR_TOOLS),
        system_prompt=SYSTEM_PROMPT,
        subagents=[
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
        # BOTH sources, and this is not a stylistic choice: skills are listed
        # per source path, so `["/skills"]` returns the four bundled ones and
        # never the store's, no matter how the route is configured. Measured
        # through the composite before and after.
        skills=list(SKILL_SOURCES),
        # The agent may READ its standing orders and may not rewrite them.
        #
        # `FilesystemBackend` implements `write` and `edit`, `create_deep_agent`
        # was called with no `permissions=`, and deepagents allows any operation
        # no rule matches (`_check_fs_permission` returns "allow" by default). So
        # the assistant could silently rewrite the four bundled SKILL.md files on
        # the live API container, and text in an ingested web page could in
        # principle instruct it to — an edit that then persists for the life of
        # the container and affects everyone using it.
        #
        # One rule closes it: matching is first-match-wins, and both `write_file`
        # and `edit_file` map to the single `"write"` operation
        # (`_DEFAULT_FS_TOOL_OPS`). Subagents inherit the parent's rules unless
        # their own spec overrides, so this covers all six in one line.
        #
        # This is a blanket deny, not the governed path. `WS-H1` opens
        # `/skills/authored/**` for writing, ledgered — and the ORDER will matter
        # then, because an allow rule ahead of this deny would reopen everything.
        # ORDER IS THE WHOLE THING. `_check_fs_permission` is first-match-wins
        # (middleware/filesystem.py:111-116), so the allow must sit ahead of the
        # deny. Swapped, the deny matches `/skills/authored/**` too and the
        # authored route is silently read-only — the feature is off, every write
        # is refused, and nothing anywhere reports a misconfiguration.
        # `test_the_bundled_skills_stay_read_only` and
        # `test_the_authored_route_is_writable` are asserted in the same run
        # precisely so neither can be satisfied by dropping the other.
        permissions=_agent_filesystem_permissions(),
        # AlephAgentMiddleware first: it wraps every tool call so an exception
        # becomes a ToolMessage the model can read and route around, instead of
        # killing the conversation. Before it, any one of 27 tools throwing
        # ended the turn — and every tool resolved its project scope OUTSIDE
        # its own try block, so even the guarded ones were guarded against the
        # wrong thing.
        middleware=[
            AlephAgentMiddleware(session_maker=_runtime.get("session_maker")),
            AuthoredSkillsMiddleware(
                session_maker=_runtime.get("session_maker"),
                actor_id=SYSTEM_ACTOR,
                backend_factory=_memory_backend,
            ),
            CopilotKitMiddleware(),
        ],
        backend=_memory_backend,
        store=store,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
    )
