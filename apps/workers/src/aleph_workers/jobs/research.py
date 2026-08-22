"""deep_research_job — runs the native research loop (WP-3).

The dispatch path (``/synthesize`` route or the bootstrap fan-out, via
``aleph_research.dispatch.dispatch_research``) creates the research
``AgentRun`` in ``pending`` (agent_kind ``deep_research`` |
``shallow_research``, input_payload ``{topic, depth, allowed_connectors}``)
and puts its id in the agent token; this job drives THAT run through
running -> succeeded/failed (the ``builder_job`` failure model: any
exception marks the run failed with ``error_text`` + ``completed_at`` —
no deferred submit, no poll timeout, no slot release).
"""

# pyright: reportMissingTypeStubs=false
# (first-party aleph_* packages ship inline types but no py.typed marker —
#  the same visible debt every other package in this repo carries)

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select

from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_db.repos.agent_events import emit_phase_completed
from aleph_observability.tracing import start_span
from aleph_research import ResearchLimits, ResearchWorkflow, resolve_bound_tools
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_workers.gateway import gateways


async def _finish_run(
    maker: Any,
    agent_run_id: UUID,
    *,
    status: str,
    result_payload: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one_or_none()
        if run is not None:
            run.status = status
            run.completed_at = utcnow()
            if result_payload is not None:
                run.result_payload = result_payload
            run.error_text = error_text
        await session.commit()


async def deep_research_job(
    ctx: dict[str, Any],
    agent_run_id_str: str,
    agent_token: str,
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
    agent_run_id = UUID(agent_run_id_str)
    maker = ctx["session_maker"]
    litellm = await gateways(ctx).litellm(claims.project_id)
    settings = ctx["settings"]
    scholar = ctx["scholar"]
    asset_store = ctx["asset_store"]
    redis_pool = ctx["redis_pool"]

    with start_span(
        "worker.deep_research",
        **{"aleph.agent_run_id": agent_run_id_str},
    ):
        # pending -> running; pull topic/depth/allowed from the run itself.
        async with maker() as session:
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
            ).scalar_one_or_none()
            if run is None:
                msg = f"agent run {agent_run_id} not found"
                raise RuntimeError(msg)
            if run.status in ("succeeded", "failed"):
                # arq re-delivered an already-terminal job (worker died after
                # the final commit but before acking). Idempotent no-op — do
                # NOT flip a genuinely succeeded run to failed.
                return {"ok": run.status == "succeeded", "error": run.error_text}
            if run.status != "pending":
                # A prior attempt moved this run to `running` and was then
                # interrupted (arq re-enqueues on timeout/hard-kill, default
                # retry_jobs=True). Re-running would duplicate ingested sources
                # and could strand the run; instead converge to a terminal
                # `failed` and return. This is the safety net that makes
                # "no strands" (Final State §6) hold across arq retries.
                prior = run.status
                run.status = "failed"
                run.error_text = f"research interrupted (prior status={prior!r}); not retried"
                run.completed_at = utcnow()
                await session.commit()
                return {"ok": False, "error": "interrupted; not retried"}
            payload = cast("dict[str, Any]", run.input_payload or {})
            topic = str(payload.get("topic") or "").strip()
            depth = str(payload.get("depth") or "deep")
            allowed_raw = payload.get("allowed_connectors")
            allowed: list[str] | None = None
            if isinstance(allowed_raw, list):
                allowed = [str(k) for k in cast("list[Any]", allowed_raw)]
            project_id: UUID = run.project_id
            profile = (
                await session.execute(
                    select(ModelProfile).where(ModelProfile.project_id == project_id)
                )
            ).scalar_one_or_none()
            profile_bindings: dict[str, Any] | None = (
                profile.bindings_jsonb if profile is not None else None
            )
            run.status = "running"
            run.started_at = utcnow()
            await session.commit()

        try:
            if not topic:
                msg = "research run has no topic"
                raise RuntimeError(msg)
            if profile_bindings is None:
                msg = f"no ModelProfile for project {project_id}"
                raise RuntimeError(msg)

            async def _on_skip(kind: str, reason: str) -> None:
                # Missing/undecryptable credential: skipped with a warning
                # event, never fatal.
                async with maker() as session:
                    await emit_phase_completed(
                        session,
                        agent_run_id=agent_run_id,
                        phase_name="research.tool_skipped",
                        duration_ms=0,
                        payload={"kind": kind, "reason": reason},
                    )
                    await session.commit()

            bound = await resolve_bound_tools(
                session_maker=maker,
                project_id=project_id,
                agent_token_secret=secret,
                auth_mode=str(getattr(settings, "aleph_auth_mode", "local")),
                allowed=allowed,
                on_skip=_on_skip,
            )
            # The bound-tool list is visible in agent-events (spec WP-3 §2).
            async with maker() as session:
                await emit_phase_completed(
                    session,
                    agent_run_id=agent_run_id,
                    phase_name="research.tools",
                    duration_ms=0,
                    payload={"bound": [t.kind for t in bound]},
                )
                await session.commit()

            limits = ResearchLimits(
                max_iterations=int(
                    getattr(settings, "research_max_iterations_shallow", 1)
                    if depth == "shallow"
                    else getattr(settings, "research_max_iterations_deep", 3)
                ),
                max_sources_per_iter=int(getattr(settings, "research_max_sources_per_iter", 6)),
                max_total_sources=int(getattr(settings, "research_max_total_sources", 15)),
            )
            workflow = ResearchWorkflow(
                session_maker=maker,
                litellm=litellm,
                principal=principal,
                scholar=scholar,
                asset_store=asset_store,
                tools=bound,
                profile_bindings=profile_bindings,
                limits=limits,
                agent_token_secret=secret,
                enqueue=redis_pool.enqueue_job,
            )
            out = await workflow.run(
                {
                    "agent_run_id": agent_run_id,
                    "project_id": project_id,
                    "topic": topic,
                    "depth": depth,
                }
            )
            result: dict[str, Any] = {
                "proposal_ids": [str(p) for p in out.get("proposal_ids") or []],
                "page_ids": [str(p) for p in out.get("committed_page_ids") or []],
                "source_count": len(out.get("ingested") or []),
            }
            status = "succeeded"
            error_text = None
        except asyncio.CancelledError:
            # arq's job_timeout (or a worker shutdown) cancels the task.
            # Mark failed best-effort, then re-raise so cancellation still
            # propagates. If this mark is itself cut short, the retry-guard
            # above (status != "pending") converges the run on the next
            # attempt — either way it never strands as `running`.
            await _finish_run(
                maker,
                agent_run_id,
                status="failed",
                result_payload=None,
                error_text="research cancelled (job timeout or worker shutdown)",
            )
            raise
        except Exception as exc:
            status = "failed"
            error_text = str(exc)[:4096]
            result = {}

        await _finish_run(
            maker,
            agent_run_id,
            status=status,
            result_payload=result or None,
            error_text=error_text,
        )
        return {"ok": status == "succeeded", **result, "error": error_text}
