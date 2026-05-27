"""LangGraph turn workflow:

  budget_gate → query_rewrite → retrieve → compose → finalize.

(Per the spec, descent is part of the retrieval node's contract, not a
separate workflow node. `composer_out.descent_requests` triggers a
re-compose inside `WikiFirstRetrievalRouter.retrieve`.)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from aleph_core.errors import BudgetExceeded
from aleph_core.time import utcnow
from aleph_db.repos.cost import get_budget
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import current_trace_id, start_span
from aleph_assistant.models import AssistantMessage
from aleph_assistant.retrieval.router import (
    RetrievalResult,
    WikiFirstRetrievalRouter,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from aleph_db.models.model_profile import ModelProfile
    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


class AssistantTurnState(TypedDict, total=False):
    # Inputs
    agent_run_id: UUID
    project_id: UUID
    thread_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    user_query: str
    prior_messages: list[AssistantMessage]
    profile_bindings: dict[str, Any]

    # Progressive
    rewritten_query: str | None
    retrieval: RetrievalResult | None
    final_status: str
    final_body: str
    error_text: str | None
    started_monotonic: float


@dataclass
class _Ctx:
    session_maker: "async_sessionmaker[AsyncSession]"
    litellm: "LiteLLMClient"
    principal: "Principal"
    profile: "ModelProfile"


_active_ctx: _Ctx | None = None


def _ctx() -> _Ctx:
    if _active_ctx is None:
        msg = "AssistantTurnWorkflow context not initialized"
        raise RuntimeError(msg)
    return _active_ctx


async def _node_budget_gate(state: AssistantTurnState) -> dict[str, Any]:
    ctx = _ctx()
    async with ctx.session_maker() as session:
        budget = await get_budget(session, state["project_id"])
        if budget is None:
            return {}
        hard = budget.cap_usd * (budget.hard_pct / Decimal("100"))
        if budget.spent_usd >= hard:
            return {
                "final_status": "budget_blocked",
                "final_body": (
                    f"Budget hard cap reached "
                    f"(${budget.spent_usd} / ${budget.cap_usd}). "
                    "Ask the project owner to raise the cap to continue."
                ),
            }
    return {}


async def _node_query_rewrite(state: AssistantTurnState) -> dict[str, Any]:
    if state.get("final_status"):
        return {}
    prior = state.get("prior_messages") or []
    if not prior:
        return {"rewritten_query": state["user_query"]}
    # Inc 2: deterministic rewrite — if the prior assistant message contains
    # a [[Foo]] wikilink and the new query is a pronoun-only reference,
    # substitute the first link. Keeps the path testable without an LLM
    # call. (Full LLM rewrite is a follow-on per the spec note.)
    q = state["user_query"].strip()
    if len(q) < 24 and any(p in q.lower() for p in (" it ", " that ", " them ")):
        last_assistant = next(
            (m for m in reversed(prior) if m.role == "assistant"), None
        )
        if last_assistant:
            import re

            m = re.search(r"\[\[([^\]]+)\]\]", last_assistant.body_md)
            if m:
                anchor = m.group(1)
                return {"rewritten_query": f"{q} (context: {anchor})"}
    return {"rewritten_query": q}


async def _node_retrieve(state: AssistantTurnState) -> dict[str, Any]:
    if state.get("final_status"):
        return {}
    ctx = _ctx()
    router = WikiFirstRetrievalRouter(
        session_maker=ctx.session_maker, litellm=ctx.litellm
    )
    result = await router.retrieve(
        principal=ctx.principal,
        project_id=state["project_id"],
        thread_id=state["thread_id"],
        query=state.get("rewritten_query") or state["user_query"],
        prior_messages=state.get("prior_messages") or [],
        profile=ctx.profile,
        agent_run_id=state["agent_run_id"],
    )
    return {"retrieval": result, "final_body": result.composed_body_md}


async def _node_finalize(state: AssistantTurnState) -> dict[str, Any]:
    ctx = _ctx()
    started = state.get("started_monotonic") or time.monotonic()
    latency_ms = int((time.monotonic() - started) * 1000)
    final_status = state.get("final_status") or "complete"
    final_body = state.get("final_body") or ""
    retrieval = state.get("retrieval")

    async with ctx.session_maker() as session:
        msg = await session.get(AssistantMessage, state["assistant_message_id"])
        if msg is not None:
            msg.body_md = final_body
            msg.status = final_status
            msg.latency_ms = latency_ms
            if retrieval is not None:
                msg.retrieval_jsonb = {
                    "selected_pages": [
                        {
                            "page_id": str(p.page_id),
                            "title": p.title,
                            "relevance": p.relevance_label,
                            "score": p.score,
                        }
                        for p in retrieval.selected_pages
                    ],
                    "expanded_pages": [
                        {
                            "page_id": str(p.page_id),
                            "title": p.title,
                        }
                        for p in retrieval.expanded_pages
                    ],
                    "descent_chunks": [
                        {
                            "chunk_id": str(c.chunk_id),
                            "source_short_id": c.source_short_id,
                            "section_path": c.section_path,
                            "score": c.score,
                        }
                        for c in retrieval.descent_chunks
                    ],
                    "descent_requests": [
                        {"source": r.source_short_id, "query": r.query_within_source}
                        for r in retrieval.descent_requests
                    ],
                    "synthesis_requests": [
                        {"concept": s.concept, "missing": s.missing}
                        for s in retrieval.synthesis_requests
                    ],
                    "coverage_judgment": retrieval.coverage_judgment,
                    "page_selection_reason": retrieval.page_selection_reason,
                }

        ledger = LedgerWriter(session)
        await ledger.append(
            project_id=state["project_id"],
            actor_id=ctx.principal.user_id,
            actor_kind=ctx.principal.actor_kind,
            action_kind=(
                "assistant.message.complete"
                if final_status == "complete"
                else f"assistant.message.{final_status}"
            ),
            target_id=state["assistant_message_id"],
            target_kind="assistant_message",
            payload={
                "thread_id": str(state["thread_id"]),
                "coverage_judgment": (
                    retrieval.coverage_judgment if retrieval else None
                ),
                "latency_ms": latency_ms,
            },
            trace_id=current_trace_id(),
        )
        await session.commit()

    return {}


class AssistantTurnWorkflow:
    def __init__(
        self,
        *,
        session_maker: "async_sessionmaker[AsyncSession]",
        litellm: "LiteLLMClient",
        principal: "Principal",
        profile: "ModelProfile",
    ) -> None:
        self._ctx = _Ctx(
            session_maker=session_maker,
            litellm=litellm,
            principal=principal,
            profile=profile,
        )
        graph: StateGraph = StateGraph(AssistantTurnState)
        graph.add_node("budget_gate", _node_budget_gate)
        graph.add_node("query_rewrite", _node_query_rewrite)
        graph.add_node("retrieve", _node_retrieve)
        graph.add_node("finalize", _node_finalize)
        graph.add_edge(START, "budget_gate")
        graph.add_edge("budget_gate", "query_rewrite")
        graph.add_edge("query_rewrite", "retrieve")
        graph.add_edge("retrieve", "finalize")
        graph.add_edge("finalize", END)
        self._compiled = graph.compile()

    async def run(self, initial: AssistantTurnState) -> AssistantTurnState:
        global _active_ctx
        _active_ctx = self._ctx
        with start_span(
            "assistant.turn",
            **{
                "aleph.project_id": str(initial["project_id"]),
                "aleph.thread_id": str(initial["thread_id"]),
            },
        ):
            initial["started_monotonic"] = time.monotonic()
            try:
                out = await self._compiled.ainvoke(initial)
                return out  # type: ignore[return-value]
            except Exception as exc:  # noqa: BLE001
                # Finalize with failure so the message lands in a clean state.
                state = dict(initial)
                state["final_status"] = "failed"
                state["error_text"] = str(exc)[:4096]
                state["final_body"] = (
                    "I hit an error mid-turn. The full trace is in Langfuse."
                )
                await _node_finalize(state)  # best-effort persistence
                raise
            finally:
                _active_ctx = None
