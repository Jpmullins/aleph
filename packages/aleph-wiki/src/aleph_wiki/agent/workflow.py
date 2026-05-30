"""LangGraph DAG for wiki ingest — 7 nodes, linear.

Pipeline (see Inc 1 §1.8):

    concept_extraction
        ↓
    alias_extraction
        ↓
    source_page_compose
        ↓
    topic_page_stubs
        ↓
    wikilink_resolve
        ↓
    wiki_index_update
        ↓
    commit_revision

Each node opens an OTEL span with `aleph.node`, `aleph.agent_kind="wiki"`,
`aleph.project_id`, `aleph.source_id`. An AgentRun row is created at start;
AgentEvent rows for phase_started/phase_completed bracket each node.

LLM calls go through `LiteLLMClient` (every cost ledgered). Workflow
state is the typed dict below; transitions are pure functions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from aleph_core.ids import uuid7
from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_db.repos.agent_events import emit_phase_completed, emit_phase_started, with_phase
from aleph_models.client import ChatMessage
from aleph_observability.tracing import start_span
from aleph_wiki.alias_service import AliasService
from aleph_wiki.feedback_service import mark_addressed, pending_for_concept
from aleph_wiki.models import RejectionFeedback, SourcePage
from aleph_wiki.wiki_service import (
    CitationDraft,
    ClaimDraft,
    WikiLinkDraft,
    WikiService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


_PROMPT_DIR = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# State + draft types
# ---------------------------------------------------------------------------


class ExtractedConcept(BaseModel):
    canonical_name: str
    surface_forms: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    salience: float = 0.5
    definition_hint: str | None = None


class ExtractedAlias(BaseModel):
    surface_form: str
    canonical_name: str
    confidence: float = 1.0


@dataclass
class WikiPageDraft:
    page_kind: str
    title: str
    slug: str
    page_id: UUID | None
    body_md: str
    summary: str
    claims: list[ClaimDraft]
    wikilinks: list[WikiLinkDraft]
    commit_message: str
    addressed_feedback_ids: list[UUID] = field(default_factory=list)


class WikiIngestState(TypedDict, total=False):
    # Inputs
    agent_run_id: UUID
    project_id: UUID
    source_id: UUID
    source_short_id: str
    source_title: str
    source_url: str | None
    connector_kind: str
    ingested_at_iso: str
    normalized_document_id: UUID
    normalized_markdown: str
    profile_bindings: dict[str, Any]

    # Progressive results
    concepts: list[ExtractedConcept]
    aliases: list[ExtractedAlias]
    source_page_draft: WikiPageDraft
    topic_page_drafts: list[WikiPageDraft]
    committed_revision_ids: list[UUID]


@dataclass
class WorkflowContext:
    """Carries the per-run side-channel dependencies — these don't fit cleanly
    into LangGraph state because they're not JSON-serializable."""

    session_maker: async_sessionmaker[AsyncSession]
    litellm: LiteLLMClient
    principal: Principal


# Mutable singletons holding the per-invocation context. LangGraph nodes are
# closures; we use a module-level reference to avoid plumbing the context
# through every state field.
_active_ctx: WorkflowContext | None = None


def _ctx() -> WorkflowContext:
    if _active_ctx is None:
        msg = "WikiIngestWorkflow context not initialized"
        raise RuntimeError(msg)
    return _active_ctx


async def _emit_compile_page(
    ctx: WorkflowContext,
    state: WikiIngestState,
    *,
    started: bool,
    page_title: str,
    page_kind: str,
    page_id: UUID | None = None,
) -> None:
    """Emit a page-scoped `compile_page` progress event for live-wiki presence.

    `started` (at compose) → the `changes` stream maps it to a `compiling` signal
    ("✦ agent is editing this page…"); the completion (at commit) → `compile_done`,
    which clears the indicator. The `wiki.revision.commit` ledger event drives the
    "updated just now" pulse + the page refetch. No-op outside an agent run.
    """
    agent_run_id = state.get("agent_run_id")
    if agent_run_id is None:
        return
    payload: dict[str, Any] = {"page_title": page_title, "page_kind": page_kind}
    if page_id is not None:
        payload["page_id"] = str(page_id)
    async with ctx.session_maker() as session:
        if started:
            await emit_phase_started(
                session, agent_run_id=agent_run_id, phase_name="compile_page", payload=payload
            )
        else:
            await emit_phase_completed(
                session,
                agent_run_id=agent_run_id,
                phase_name="compile_page",
                duration_ms=0,
                payload=payload,
            )
        await session.commit()


# ---------------------------------------------------------------------------
# JSON-schema-constrained chat helper
# ---------------------------------------------------------------------------


async def _call_llm_json(
    *,
    project_id: UUID,
    agent_run_id: UUID,
    capability: Capability,
    profile_bindings: dict,
    system_prompt: str,
    user_payload: str,
    purpose: str,
) -> dict:
    ctx = _ctx()
    resp = await ctx.litellm.chat(
        principal=ctx.principal,
        project_id=project_id,
        agent_run_id=agent_run_id,
        capability=capability,
        profile_bindings=profile_bindings,
        messages=[
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_payload),
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=4096,
        purpose=purpose,
    )
    if not resp.choices:
        return {}
    content = resp.choices[0].message.content or ""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Be tolerant — find first JSON object substring.
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@with_phase("concept_extraction", ctx_getter=lambda: _ctx())
async def _node_concept_extraction(state: WikiIngestState) -> dict:
    with start_span(
        "wiki.node.concept_extraction",
        **{
            "aleph.node": "concept_extraction",
            "aleph.agent_kind": "wiki",
            "aleph.project_id": str(state["project_id"]),
            "aleph.source_id": str(state["source_id"]),
        },
    ):
        body = state["normalized_markdown"]
        user_payload = (
            f"Source title: {state['source_title']}\n\nDocument body (markdown):\n\n{body[:60000]}"
        )
        out = await _call_llm_json(
            project_id=state["project_id"],
            agent_run_id=state["agent_run_id"],
            capability=Capability.EXTRACTION,
            profile_bindings=state["profile_bindings"],
            system_prompt=_prompt("concept_extraction"),
            user_payload=user_payload,
            purpose="wiki.concept_extraction",
        )
        raw_list = out.get("concepts") or []
        concepts: list[ExtractedConcept] = []
        for item in raw_list:
            try:
                concepts.append(ExtractedConcept.model_validate(item))
            except Exception:
                continue
        return {"concepts": concepts}


@with_phase("alias_extraction", ctx_getter=lambda: _ctx())
async def _node_alias_extraction(state: WikiIngestState) -> dict:
    with start_span(
        "wiki.node.alias_extraction",
        **{
            "aleph.node": "alias_extraction",
            "aleph.agent_kind": "wiki",
            "aleph.project_id": str(state["project_id"]),
            "aleph.source_id": str(state["source_id"]),
        },
    ):
        concepts = state.get("concepts") or []
        if not concepts:
            return {"aliases": []}
        payload = json.dumps(
            {
                "concepts": [c.model_dump() for c in concepts],
            },
            indent=2,
        )
        out = await _call_llm_json(
            project_id=state["project_id"],
            agent_run_id=state["agent_run_id"],
            capability=Capability.EXTRACTION,
            profile_bindings=state["profile_bindings"],
            system_prompt=_prompt("alias_extraction"),
            user_payload=payload,
            purpose="wiki.alias_extraction",
        )
        raw = out.get("aliases") or []
        aliases: list[ExtractedAlias] = []
        for item in raw:
            try:
                aliases.append(ExtractedAlias.model_validate(item))
            except Exception:
                continue

        # Persist via AliasService.
        ctx = _ctx()
        async with ctx.session_maker() as session:
            svc = AliasService(session)
            for a in aliases:
                if a.confidence < 0.5:
                    continue
                await svc.upsert(
                    project_id=state["project_id"],
                    surface_form=a.surface_form,
                    canonical_name=a.canonical_name,
                    confidence=a.confidence,
                    created_by=ctx.principal.user_id,
                )
            # Also record surface_form ↔ canonical_name pairs from concept lists.
            for c in concepts:
                for sf in c.surface_forms:
                    if sf and sf != c.canonical_name:
                        await svc.upsert(
                            project_id=state["project_id"],
                            surface_form=sf,
                            canonical_name=c.canonical_name,
                            confidence=c.confidence,
                            created_by=ctx.principal.user_id,
                        )
            await session.commit()
        return {"aliases": aliases}


@with_phase("source_page_compose", ctx_getter=lambda: _ctx())
async def _node_source_page_compose(state: WikiIngestState) -> dict:
    with start_span(
        "wiki.node.source_page_compose",
        **{
            "aleph.node": "source_page_compose",
            "aleph.agent_kind": "wiki",
            "aleph.project_id": str(state["project_id"]),
            "aleph.source_id": str(state["source_id"]),
        },
    ):
        # Pull rejection feedback for THIS source concept.
        ctx = _ctx()
        # Live-wiki presence: signal that the agent has started composing this
        # page (lasts through the multi-second LLM compose). Keyed by title since
        # the page row may not exist yet; the commit signal carries the page_id.
        await _emit_compile_page(
            ctx,
            state,
            started=True,
            page_title=state["source_title"],
            page_kind="source",
        )
        rejections: list[RejectionFeedback] = []
        async with ctx.session_maker() as session:
            rejections = await pending_for_concept(
                session,
                project_id=state["project_id"],
                concept_name=f"Source:{state['source_short_id']}",
            )

        concept_lines = "\n".join(
            f"- {c.canonical_name}: {c.definition_hint or ''}"
            for c in state.get("concepts", [])[:30]
        )
        rejection_block = ""
        if rejections:
            rejection_block = (
                "\n\nRejection feedback for this source (address these):\n"
                + "\n".join(f"- {r.reason}" for r in rejections)
            )

        payload = (
            f"Source short_id: {state['source_short_id']}\n"
            f"Source title: {state['source_title']}\n"
            f"Connector: {state['connector_kind']}\n"
            f"URL: {state.get('source_url') or 'none'}\n"
            f"Ingested: {state['ingested_at_iso']}\n\n"
            f"Concepts in this source:\n{concept_lines}\n\n"
            "Document body (markdown):\n\n"
            f"{state['normalized_markdown'][:50000]}"
            f"{rejection_block}"
        )

        ctx_chat = await _call_llm_json(
            project_id=state["project_id"],
            agent_run_id=state["agent_run_id"],
            capability=Capability.SYNTHESIS,
            profile_bindings=state["profile_bindings"],
            system_prompt=_prompt("source_page_compose")
            + '\n\nReturn JSON: {"body_md": "...", "summary": "...", '
            + '"claims": [{"text": "...", "citation_marker": "[c1]"}]}',
            user_payload=payload,
            purpose="wiki.source_page_compose",
        )
        body_md = ctx_chat.get("body_md") or _fallback_source_page(state)
        summary = ctx_chat.get("summary") or f"Source page for {state['source_title']}."
        raw_claims = ctx_chat.get("claims") or []
        claims: list[ClaimDraft] = []
        for c in raw_claims:
            text = (c.get("text") or "").strip()
            marker = (c.get("citation_marker") or "").strip() or "[c?]"
            if not text:
                continue
            claims.append(
                ClaimDraft(
                    text=text,
                    confidence="cited",
                    section_anchor="key-claims",
                    citations=[
                        CitationDraft(
                            chunk_ids=[],  # Inc 1 binds claims to the source page itself.
                            source_page_id=None,  # Set in commit step (self-citation).
                            citation_marker=marker,
                        )
                    ],
                )
            )

        wikilinks = _wikilinks_from_body(body_md)
        draft = WikiPageDraft(
            page_kind="source",
            title=f"Source: {state['source_title']}",
            slug=f"source-{state['source_short_id'].lower()}",
            page_id=None,
            body_md=body_md,
            summary=summary,
            claims=claims,
            wikilinks=wikilinks,
            commit_message=f"Ingest source {state['source_short_id']}",
            addressed_feedback_ids=[r.id for r in rejections],
        )
        return {"source_page_draft": draft}


def _fallback_source_page(state: WikiIngestState) -> str:
    return (
        f"# {state['source_title']}\n\n"
        "## Provenance\n"
        f"- Identifier: [[Source:{state['source_short_id']}]]\n"
        f"- Connector: {state['connector_kind']}\n"
        f"- URL: {state.get('source_url') or 'none'}\n"
        f"- Ingested: {state['ingested_at_iso']}\n\n"
        "## Summary\n"
        "(Composition unavailable — see the original asset.)\n\n"
        "## Concepts covered\n\n"
        "## Key claims\n"
    )


def _wikilinks_from_body(body_md: str) -> list[WikiLinkDraft]:
    import re

    counts: dict[str, int] = {}
    for m in re.finditer(r"\[\[([^\]]+)\]\]", body_md):
        title = m.group(1).split("|", 1)[0].strip()
        counts[title] = counts.get(title, 0) + 1
    return [WikiLinkDraft(dst_title=t, dst_page_id=None, occurrences=n) for t, n in counts.items()]


@with_phase("topic_page_stubs", ctx_getter=lambda: _ctx())
async def _node_topic_page_stubs(state: WikiIngestState) -> dict:
    with start_span(
        "wiki.node.topic_page_stubs",
        **{
            "aleph.node": "topic_page_stubs",
            "aleph.agent_kind": "wiki",
            "aleph.project_id": str(state["project_id"]),
            "aleph.source_id": str(state["source_id"]),
        },
    ):
        concepts = state.get("concepts") or []
        drafts: list[WikiPageDraft] = []
        for c in concepts:
            payload = (
                f"Concept canonical name: {c.canonical_name}\n"
                f"Surface forms: {', '.join(c.surface_forms)}\n"
                f"Definition hint: {c.definition_hint or '(none)'}\n"
                f"Source short_id: {state['source_short_id']}\n"
                f"Source title: {state['source_title']}\n\n"
                "Document excerpt (for context):\n"
                f"{state['normalized_markdown'][:8000]}"
            )
            out = await _call_llm_json(
                project_id=state["project_id"],
                agent_run_id=state["agent_run_id"],
                capability=Capability.EXTRACTION,
                profile_bindings=state["profile_bindings"],
                system_prompt=_prompt("topic_page_stub")
                + '\n\nReturn JSON: {"body_md": "...", "summary": "..."}',
                user_payload=payload,
                purpose="wiki.topic_page_stub",
            )
            body_md = out.get("body_md") or _fallback_topic_stub(c, state)
            summary = out.get("summary") or f"Stub page for {c.canonical_name}."
            drafts.append(
                WikiPageDraft(
                    page_kind="stub",
                    title=c.canonical_name,
                    slug=_slugify_concept(c.canonical_name),
                    page_id=None,
                    body_md=body_md,
                    summary=summary,
                    claims=[],
                    wikilinks=_wikilinks_from_body(body_md),
                    commit_message=(f"Stub from source {state['source_short_id']}"),
                )
            )
        return {"topic_page_drafts": drafts}


def _fallback_topic_stub(c: ExtractedConcept, state: WikiIngestState) -> str:
    return (
        f"# {c.canonical_name}\n\n"
        "## Definition\n"
        f"{c.definition_hint or '(Definition not yet extracted.)'}\n\n"
        "## Mentioned in\n"
        f"- [[Source:{state['source_short_id']}]] — {state['source_title']}\n"
    )


def _slugify_concept(name: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:96] or "concept"


@with_phase("wikilink_resolve", ctx_getter=lambda: _ctx())
async def _node_wikilink_resolve(state: WikiIngestState) -> dict:
    with start_span(
        "wiki.node.wikilink_resolve",
        **{
            "aleph.node": "wikilink_resolve",
            "aleph.agent_kind": "wiki",
            "aleph.project_id": str(state["project_id"]),
            "aleph.source_id": str(state["source_id"]),
        },
    ):
        ctx = _ctx()
        source_draft = state["source_page_draft"]
        drafts = state.get("topic_page_drafts") or []

        async with ctx.session_maker() as session:
            svc = AliasService(session)
            for draft in (source_draft, *drafts):
                resolved: list[WikiLinkDraft] = []
                for link in draft.wikilinks:
                    r = await svc.resolve(
                        project_id=state["project_id"],
                        surface_form=link.dst_title,
                    )
                    resolved.append(
                        WikiLinkDraft(
                            dst_title=link.dst_title,
                            dst_page_id=r.canonical_page_id if r else None,
                            occurrences=link.occurrences,
                        )
                    )
                draft.wikilinks = resolved
        return {}


@with_phase("commit_revision", ctx_getter=lambda: _ctx())
async def _node_commit_revision(state: WikiIngestState) -> dict:
    with start_span(
        "wiki.node.commit_revision",
        **{
            "aleph.node": "commit_revision",
            "aleph.agent_kind": "wiki",
            "aleph.project_id": str(state["project_id"]),
            "aleph.source_id": str(state["source_id"]),
        },
    ):
        ctx = _ctx()
        committed: list[UUID] = []

        async with ctx.session_maker() as session:
            from aleph_db.repos.ledger import LedgerWriter

            ledger = LedgerWriter(session)
            svc = WikiService(session)

            # Source page first.
            sd = state["source_page_draft"]
            result = await svc.commit_revision(
                principal=ctx.principal,
                ledger=ledger,
                project_id=state["project_id"],
                page_id=sd.page_id,
                title=sd.title,
                slug=sd.slug,
                page_kind="source",
                body_md=sd.body_md,
                summary=sd.summary,
                claims=sd.claims,
                wikilinks=sd.wikilinks,
                commit_message=sd.commit_message,
                respect_hand_edits=True,
            )
            committed.append(result.revision_id)
            # Bridge SourcePage row.
            existing_sp = await session.get(SourcePage, result.page_id)
            if existing_sp is None:
                # We index by (source_id, page_id) UNIQUE — explicit insert if missing.
                from sqlalchemy import select as _select

                existing = (
                    await session.execute(
                        _select(SourcePage).where(SourcePage.source_id == state["source_id"])
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        SourcePage(
                            id=uuid7(),
                            project_id=state["project_id"],
                            source_id=state["source_id"],
                            page_id=result.page_id,
                            extracted_claims_jsonb=[
                                {
                                    "text": c.text,
                                    "marker": (
                                        c.citations[0].citation_marker if c.citations else ""
                                    ),
                                }
                                for c in sd.claims
                            ],
                            extracted_at=utcnow(),
                        )
                    )

            # Topic stubs.
            for draft in state.get("topic_page_drafts", []):
                r = await svc.commit_revision(
                    principal=ctx.principal,
                    ledger=ledger,
                    project_id=state["project_id"],
                    page_id=draft.page_id,
                    title=draft.title,
                    slug=draft.slug,
                    page_kind="stub",
                    body_md=draft.body_md,
                    summary=draft.summary,
                    claims=draft.claims,
                    wikilinks=draft.wikilinks,
                    commit_message=draft.commit_message,
                    respect_hand_edits=True,
                )
                committed.append(r.revision_id)

            # Repair any broken links now that the new pages exist.
            await AliasService(session).repair_broken_links(project_id=state["project_id"])

            # Mark addressed rejection feedback.
            if sd.addressed_feedback_ids:
                await mark_addressed(
                    session,
                    feedback_ids=sd.addressed_feedback_ids,
                    revision_id=committed[0],
                )

            await session.commit()

        # Live-wiki presence: the source page is committed → clear the "editing"
        # indicator (the `wiki.revision.commit` ledger event already drives the
        # pulse + refetch). Emitted after commit so the page row exists.
        await _emit_compile_page(
            ctx,
            state,
            started=False,
            page_title=sd.title,
            page_kind="source",
            page_id=result.page_id,
        )
        return {"committed_revision_ids": committed}


@with_phase("wiki_index_update", ctx_getter=lambda: _ctx())
async def _node_wiki_index_update(state: WikiIngestState) -> dict:
    with start_span(
        "wiki.node.wiki_index_update",
        **{
            "aleph.node": "wiki_index_update",
            "aleph.agent_kind": "wiki",
            "aleph.project_id": str(state["project_id"]),
            "aleph.source_id": str(state["source_id"]),
        },
    ):
        # IndexService.refresh_page is called inside wiki_service.commit_revision
        # for every committed page, so this node is structural — it confirms
        # the index is up to date and serves as the documented step in the DAG.
        return {}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


class WikiIngestWorkflow:
    """Builds and runs the 7-node LangGraph DAG."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        litellm: LiteLLMClient,
        principal: Principal,
    ) -> None:
        self._ctx = WorkflowContext(
            session_maker=session_maker, litellm=litellm, principal=principal
        )
        graph: StateGraph = StateGraph(WikiIngestState)
        graph.add_node("concept_extraction", _node_concept_extraction)
        graph.add_node("alias_extraction", _node_alias_extraction)
        graph.add_node("source_page_compose", _node_source_page_compose)
        graph.add_node("topic_page_stubs", _node_topic_page_stubs)
        graph.add_node("wikilink_resolve", _node_wikilink_resolve)
        graph.add_node("wiki_index_update", _node_wiki_index_update)
        graph.add_node("commit_revision", _node_commit_revision)

        graph.add_edge(START, "concept_extraction")
        graph.add_edge("concept_extraction", "alias_extraction")
        graph.add_edge("alias_extraction", "source_page_compose")
        graph.add_edge("source_page_compose", "topic_page_stubs")
        graph.add_edge("topic_page_stubs", "wikilink_resolve")
        graph.add_edge("wikilink_resolve", "commit_revision")
        graph.add_edge("commit_revision", "wiki_index_update")
        graph.add_edge("wiki_index_update", END)

        self._compiled = graph.compile()

    async def run(self, initial_state: WikiIngestState) -> WikiIngestState:
        global _active_ctx
        _active_ctx = self._ctx
        try:
            out = await self._compiled.ainvoke(initial_state)
            return out  # type: ignore[return-value]
        finally:
            _active_ctx = None
