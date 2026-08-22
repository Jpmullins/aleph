"""bootstrap_project_job: seed a project's wiki from its title + description.

Phase 1 ``scope``    — one synthesis LLM call → {overview_md, seed_topics[]}.
Phase 2 ``seed_overview`` — commit a draft 'overview' wiki page (instant content).
Phase 3 ``dispatch_research`` — fan out the native research→synthesis pipeline
                       per seed topic (bounded by settings.bootstrap_max_topics);
                       each is a child run that lands a draft page via
                       deep_research_job.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_db.repos.agent_events import (
    emit_phase_completed,
    emit_phase_failed,
    emit_phase_started,
)
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.client import ChatMessage
from aleph_observability.tracing import start_span
from aleph_research.dispatch import dispatch_research
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_wiki.wiki_service import WikiLinkDraft, WikiService
from aleph_workers.gateway import gateways

_SCOPE_SYS = (
    "You are bootstrapping a research wiki for a new investigation. Given the "
    "project title and description, return ONLY a raw JSON object (no prose, no "
    "markdown code fences) with exactly two keys: "
    '"overview_md" (a 2-4 paragraph markdown overview framing the research scope; '
    'mention each seed topic as a [[wikilink]]) and "seed_topics" (an array of '
    "at most {max_topics} concise, distinct topic titles suitable as wiki page "
    "names — proper nouns / concepts, not sentences).\n\n"
    "CRITICAL — stay on the EXACT subject of the title. Every seed topic must be "
    "a specific sub-area of the precise subject stated in the title, NOT a broader "
    "field, adjacent area, or general survey. If the description lists facets "
    "(e.g. applications, algorithms, gaps, forecasts), make each seed topic a "
    "concrete instance of one of those facets applied to the EXACT subject — "
    "qualified by the subject, never the facet in general. Example: for a project "
    "on 'forecasting of X', a topic must be about forecasting X specifically (not "
    "'forecasting' in general). Prefer narrow, on-topic titles over broad ones."
)

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

# Seconds between successive seed-topic research dispatches (arq _defer_by), so
# a multi-topic bootstrap doesn't run its shallow runs concurrently and
# 429-storm the keyless scholarly APIs. A shallow run is ~60-120s; ~90s spacing
# keeps at most ~1 hitting the scholarly endpoints at a time.
_RESEARCH_STAGGER_S = 90


def _loads_lenient(text: str) -> dict[str, Any]:
    """Recover a JSON object from an LLM response.

    Bedrock/Anthropic via the gateway does not reliably honor
    ``response_format={"type": "json_object"}``, so the model may wrap the JSON
    in ``` fences or prefix it with prose. Try a direct parse, then the first
    ``{...}`` span. Returns ``{}`` if nothing parses (caller falls back to using
    the raw text as the overview body).
    """
    s = (text or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJ_RE.search(s)
    if m is None:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _set_status(maker: Any, run_id: UUID, status: str, error: str | None = None) -> None:
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return
        run.status = status
        if status in ("succeeded", "failed"):
            run.completed_at = utcnow()
        if error:
            run.error_text = error[:4096]
        await session.commit()


async def bootstrap_project_job(
    ctx: dict[str, Any], project_id_str: str, agent_run_id_str: str, agent_token: str
) -> dict[str, Any]:
    secret: str = ctx["agent_token_secret"]
    claims = verify_agent_token(agent_token, secret=secret)
    principal = Principal(
        user_id=claims.user_id,
        subject="agent",
        email="",
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )
    project_id = UUID(project_id_str)
    agent_run_id = UUID(agent_run_id_str)
    maker = ctx["session_maker"]
    litellm = await gateways(ctx).litellm(project_id)
    settings = ctx["settings"]
    redis_pool = ctx["redis_pool"]
    max_topics = int(getattr(settings, "bootstrap_max_topics", 3))
    depth = str(getattr(settings, "bootstrap_depth", "shallow"))

    with start_span(
        "worker.bootstrap",
        **{"aleph.project_id": project_id_str, "aleph.agent_run_id": agent_run_id_str},
    ):
        await _set_status(maker, agent_run_id, "running")
        try:
            # --- Phase 1: scope ---
            async with maker() as session:
                project = (
                    await session.execute(select(Project).where(Project.id == project_id))
                ).scalar_one_or_none()
                profile = (
                    await session.execute(
                        select(ModelProfile).where(ModelProfile.project_id == project_id)
                    )
                ).scalar_one_or_none()
                if project is None or profile is None:
                    msg = "project or model profile missing"
                    raise RuntimeError(msg)
                title, description = project.title, project.description or ""
                await emit_phase_started(session, agent_run_id=agent_run_id, phase_name="scope")
                await session.commit()

            t0 = time.monotonic()
            resp = await litellm.chat(
                principal=principal,
                project_id=project_id,
                agent_run_id=agent_run_id,
                capability=Capability.SYNTHESIS,
                profile_bindings=profile.bindings_jsonb,
                messages=[
                    ChatMessage(role="system", content=_SCOPE_SYS.format(max_topics=max_topics)),
                    ChatMessage(role="user", content=f"Title: {title}\nDescription: {description}"),
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=1500,
                purpose="bootstrap.scope",
            )
            content = (resp.choices[0].message.content if resp.choices else "") or ""
            parsed = _loads_lenient(content)
            # If JSON couldn't be recovered, use the raw model text as the
            # overview body so the page still seeds (degraded: no seed topics).
            overview_md = str(
                parsed.get("overview_md") or content or f"# {title}\n\n{description}"
            ).strip()
            seed_topics = [
                str(t).strip() for t in (parsed.get("seed_topics") or []) if str(t).strip()
            ][:max_topics]
            async with maker() as session:
                await emit_phase_completed(
                    session,
                    agent_run_id=agent_run_id,
                    phase_name="scope",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    payload={"seed_topics": seed_topics},
                )
                await session.commit()

            # --- Phase 2: seed overview page (draft, instant) ---
            async with maker() as session:
                await emit_phase_started(
                    session, agent_run_id=agent_run_id, phase_name="seed_overview"
                )
                ledger = LedgerWriter(session)
                svc = WikiService(session)
                wikilinks = [
                    WikiLinkDraft(
                        dst_title=t,
                        dst_page_id=None,
                        occurrences=max(1, overview_md.count(f"[[{t}]]")),
                    )
                    for t in seed_topics
                ]
                result = await svc.commit_revision(
                    principal=principal,
                    ledger=ledger,
                    project_id=project_id,
                    page_id=None,
                    title=title,
                    slug=None,
                    page_kind="overview",
                    body_md=overview_md,
                    summary=(description or overview_md)[:2048],
                    claims=[],
                    wikilinks=wikilinks,
                    commit_message="Bootstrap: project overview",
                    respect_hand_edits=True,
                )
                await emit_phase_completed(
                    session,
                    agent_run_id=agent_run_id,
                    phase_name="seed_overview",
                    duration_ms=0,
                    payload={"page_id": str(result.page_id)},
                )
                await session.commit()
                overview_page_id = result.page_id

            # Knit the overview (register its alias + cross-link) now that it
            # exists. Seed-topic links repair as each topic page lands via its
            # own synthesis-triggered curate.
            await redis_pool.enqueue_job("curate_page_job", str(project_id), str(overview_page_id))

            # --- Phase 3: dispatch research per seed topic ---
            dispatched = 0
            async with maker() as session:
                await emit_phase_started(
                    session, agent_run_id=agent_run_id, phase_name="dispatch_research"
                )
                ledger = LedgerWriter(session)
                # Stagger the fan-out: running every seed topic's research at
                # once makes them 429-storm the shared keyless scholarly APIs
                # (OpenAlex/Semantic Scholar). Spacing starts by _RESEARCH_STAGGER_S
                # keeps concurrent scholarly load low so each run finds sources.
                for i, topic in enumerate(seed_topics):
                    try:
                        r = await dispatch_research(
                            session=session,
                            ledger=ledger,
                            redis_pool=redis_pool,
                            agent_token_secret=secret,
                            project_id=project_id,
                            principal_user_id=principal.user_id,
                            actor_kind=principal.actor_kind,
                            topic=topic,
                            depth=depth,
                            defer_seconds=i * _RESEARCH_STAGGER_S,
                        )
                        if r.dispatched:
                            dispatched += 1
                    except ValueError:
                        break  # no connectors enabled — overview page still stands
                await emit_phase_completed(
                    session,
                    agent_run_id=agent_run_id,
                    phase_name="dispatch_research",
                    duration_ms=0,
                    payload={"dispatched": dispatched, "topics": len(seed_topics)},
                )
                await session.commit()

            await _set_status(maker, agent_run_id, "succeeded")
            return {"status": "succeeded", "seed_topics": seed_topics, "dispatched": dispatched}
        except Exception as exc:
            async with maker() as session:
                await emit_phase_failed(
                    session, agent_run_id=agent_run_id, phase_name="bootstrap", error_text=str(exc)
                )
                await session.commit()
            await _set_status(maker, agent_run_id, "failed", error=str(exc))
            raise
