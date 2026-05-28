"""EditorialReviewer workflow.

Subagents (run in series for Inc 5; can parallelize later):
  contradiction → weak_source → narrative_gap → coverage_gap → factual_freshness.

Each produces ReviewFindings of its kind. Findings with `severity ≥ medium`
gain a paired `ApprovalRequest` for analyst review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage
from aleph_observability.tracing import start_span
from aleph_reviewer.approval_service import create_request
from aleph_reviewer.review_service import (
    add_finding,
    finalize_run,
    start_run,
)
from aleph_wiki.models import WikiClaim, WikiPage, WikiRevision

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_db.models.model_profile import ModelProfile
    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


@dataclass
class _Ctx:
    session_maker: "async_sessionmaker[AsyncSession]"
    litellm: "LiteLLMClient"
    principal: "Principal"
    profile: "ModelProfile"


_active_ctx: _Ctx | None = None


def _ctx() -> _Ctx:
    if _active_ctx is None:
        msg = "EditorialReviewerWorkflow context not initialized"
        raise RuntimeError(msg)
    return _active_ctx


class EditorialReviewState(TypedDict, total=False):
    project_id: UUID
    review_run_id: UUID
    agent_run_id: UUID
    finding_count: int


_FINDING_SCHEMA = (
    """Return JSON:
{
  "findings": [
    {
      "kind": "contradiction|weak_source|narrative_gap|coverage_gap|factual_staleness",
      "severity": "info|low|medium|high|critical",
      "title": "...",
      "description": "...",
      "target_page_id": "<uuid or null>",
      "evidence_refs": [{"kind": "claim|source|page", "id": "...", "label": "..."}]
    }
  ]
}"""
)


async def _run_subagent(
    state: EditorialReviewState,
    *,
    name: str,
    system_prompt: str,
    sample_payload: dict[str, Any],
) -> int:
    ctx = _ctx()
    with start_span(f"review.editorial.{name}", **{"aleph.subagent": name}):
        resp = await ctx.litellm.chat(
            principal=ctx.principal,
            project_id=state["project_id"],
            agent_run_id=state["agent_run_id"],
            capability=Capability.SYNTHESIS,
            profile_bindings=ctx.profile.bindings_jsonb,
            messages=[
                ChatMessage(role="system", content=system_prompt + "\n\n" + _FINDING_SCHEMA),
                ChatMessage(role="user", content=json.dumps(sample_payload, indent=2)),
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=4096,
            purpose=f"editorial.{name}",
        )
        if not resp.choices:
            return 0
        try:
            parsed = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            return 0
        raw = parsed.get("findings") or []
        n = 0
        async with ctx.session_maker() as session:
            for f in raw:
                if not isinstance(f, dict):
                    continue
                kind = f.get("kind") or name
                severity = f.get("severity") or "low"
                title = f.get("title") or kind
                description = f.get("description") or ""
                target_page_id = f.get("target_page_id")
                evidence_refs = f.get("evidence_refs") or []
                target_uuid: UUID | None
                try:
                    target_uuid = UUID(target_page_id) if target_page_id else None
                except (TypeError, ValueError):
                    target_uuid = None
                finding = await add_finding(
                    session,
                    review_run_id=state["review_run_id"],
                    project_id=state["project_id"],
                    finding_kind=kind,
                    severity=severity,
                    title=title[:255],
                    description=description,
                    target_page_id=target_uuid,
                    evidence_refs=evidence_refs if isinstance(evidence_refs, list) else [],
                    auto_resolvable=False,
                    created_by=ctx.principal.user_id,
                )
                # Medium+ findings get an ApprovalRequest paired.
                if severity in ("medium", "high", "critical"):
                    req = await create_request(
                        session,
                        project_id=state["project_id"],
                        target_kind="review_finding",
                        target_id=finding.id,
                        title=title[:255],
                        summary=description,
                        severity=severity,
                        proposed_patch=None,
                        evidence_refs=evidence_refs if isinstance(evidence_refs, list) else [],
                        requested_by=ctx.principal,
                    )
                    finding.approval_request_id = req.id
                n += 1
            await session.commit()
    return n


async def _sample_payload(state: EditorialReviewState) -> dict[str, Any]:
    """Build the small project payload each editorial subagent sees."""
    ctx = _ctx()
    async with ctx.session_maker() as session:
        pages = list(
            (
                await session.execute(
                    select(WikiPage)
                    .where(WikiPage.project_id == state["project_id"])
                    .order_by(WikiPage.last_compiled_at.desc().nullslast())
                    .limit(25)
                )
            )
            .scalars()
            .all()
        )
        rev_ids = [p.current_revision_id for p in pages if p.current_revision_id]
        revisions = (
            list(
                (
                    await session.execute(
                        select(WikiRevision).where(WikiRevision.id.in_(rev_ids))
                    )
                ).scalars().all()
            )
            if rev_ids
            else []
        )
        rev_by_id = {r.id: r for r in revisions}
        return {
            "pages": [
                {
                    "id": str(p.id),
                    "title": p.title,
                    "page_kind": p.page_kind,
                    "summary": (
                        rev_by_id[p.current_revision_id].summary
                        if p.current_revision_id and p.current_revision_id in rev_by_id
                        else ""
                    ),
                    "body_excerpt": (
                        rev_by_id[p.current_revision_id].body_md[:4000]
                        if p.current_revision_id and p.current_revision_id in rev_by_id
                        else ""
                    ),
                }
                for p in pages
            ],
        }


async def _node_contradiction(state: EditorialReviewState) -> dict[str, int]:
    n = await _run_subagent(
        state,
        name="contradiction",
        system_prompt=(
            "You are the contradiction detector. Read the provided wiki pages "
            "and identify pages whose claims contradict each other. Cite both "
            "pages in evidence_refs."
        ),
        sample_payload=await _sample_payload(state),
    )
    return {"n": n}


async def _node_weak_source(state: EditorialReviewState) -> dict[str, int]:
    n = await _run_subagent(
        state,
        name="weak_source",
        system_prompt=(
            "You are the weak-source detector. Flag claims that cite low-quality "
            "or unsuitable sources for the project's domain."
        ),
        sample_payload=await _sample_payload(state),
    )
    return {"n": n}


async def _node_narrative_gap(state: EditorialReviewState) -> dict[str, int]:
    n = await _run_subagent(
        state,
        name="narrative_gap",
        system_prompt=(
            "You are the narrative-gap detector. Find pages with abrupt "
            "transitions, missing context, or undefined terms."
        ),
        sample_payload=await _sample_payload(state),
    )
    return {"n": n}


async def _node_coverage_gap(state: EditorialReviewState) -> dict[str, int]:
    n = await _run_subagent(
        state,
        name="coverage_gap",
        system_prompt=(
            "You are the coverage-gap detector. Identify topics referenced as "
            "[[wikilinks]] but lacking pages of meaningful depth."
        ),
        sample_payload=await _sample_payload(state),
    )
    return {"n": n}


async def _node_factual_freshness(state: EditorialReviewState) -> dict[str, int]:
    n = await _run_subagent(
        state,
        name="factual_freshness",
        system_prompt=(
            "You are the freshness reviewer. Flag claims whose underlying "
            "sources are older than what the project requires for currency."
        ),
        sample_payload=await _sample_payload(state),
    )
    return {"n": n}


async def _node_finalize(state: EditorialReviewState) -> dict[str, int]:
    total = (
        (state.get("n_c") or 0)  # type: ignore[arg-type]
        + (state.get("n_w") or 0)  # type: ignore[arg-type]
        + (state.get("n_n") or 0)  # type: ignore[arg-type]
        + (state.get("n_g") or 0)  # type: ignore[arg-type]
        + (state.get("n_f") or 0)  # type: ignore[arg-type]
    )
    ctx = _ctx()
    async with ctx.session_maker() as session:
        await finalize_run(
            session,
            run_id=state["review_run_id"],
            status="completed",
            finding_count=total,
        )
        await session.commit()
    return {"finding_count": total}


def _wrap(handler, slot: str):
    async def _wrapped(state):  # type: ignore[no-untyped-def]
        out = await handler(state)
        return {slot: out.get("n", 0)}

    return _wrapped


class EditorialReviewerWorkflow:
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
        graph: StateGraph = StateGraph(EditorialReviewState)
        graph.add_node("contradiction", _wrap(_node_contradiction, "n_c"))
        graph.add_node("weak_source", _wrap(_node_weak_source, "n_w"))
        graph.add_node("narrative_gap", _wrap(_node_narrative_gap, "n_n"))
        graph.add_node("coverage_gap", _wrap(_node_coverage_gap, "n_g"))
        graph.add_node("factual_freshness", _wrap(_node_factual_freshness, "n_f"))
        graph.add_node("finalize", _node_finalize)
        graph.add_edge(START, "contradiction")
        graph.add_edge("contradiction", "weak_source")
        graph.add_edge("weak_source", "narrative_gap")
        graph.add_edge("narrative_gap", "coverage_gap")
        graph.add_edge("coverage_gap", "factual_freshness")
        graph.add_edge("factual_freshness", "finalize")
        graph.add_edge("finalize", END)
        self._compiled = graph.compile()

    async def run(self, *, project_id: UUID, agent_run_id: UUID, trigger: str) -> int:
        global _active_ctx
        _active_ctx = self._ctx
        async with self._ctx.session_maker() as session:
            run = await start_run(
                session,
                project_id=project_id,
                kind="editorial",
                trigger=trigger,
                target_revision_id=None,
                target_scope="project",
                agent_run_id=agent_run_id,
                created_by=self._ctx.principal.user_id,
            )
            await session.commit()
            review_run_id = run.id
        try:
            state: EditorialReviewState = {
                "project_id": project_id,
                "review_run_id": review_run_id,
                "agent_run_id": agent_run_id,
            }
            out = await self._compiled.ainvoke(state)
            return int(out.get("finding_count", 0))  # type: ignore[arg-type]
        finally:
            _active_ctx = None
