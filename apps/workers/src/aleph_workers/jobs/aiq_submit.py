"""aiq_submit_job: deferred AIQ research submission behind the in-flight gate.

When ``_dispatch_core`` finds the AIQ gate full (too many research jobs
already running inside aiq-server), it enqueues this job instead of
submitting inline. Each invocation retries the gate once: acquired → dispatch
to AIQ + enqueue the poll job; still full (or AIQ briefly down) → re-enqueue
itself deferred; attempts exhausted → mark the AgentRun failed. The core
logic lives in ``aleph_aiq.dispatch.submit_core``; this wrapper verifies the
agent token and translates the outcome into AgentRun status + AgentEvents.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_aiq.dispatch import SubmitOutcome, submit_core
from aleph_aiq.job_service import append_aiq_event
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_observability.tracing import start_span
from aleph_security.agent_token import verify_agent_token


async def _mark_failed(maker: Any, agent_run_id: UUID, error: str) -> None:
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one_or_none()
        if run is not None:
            run.status = "failed"
            run.completed_at = utcnow()
            run.error_text = error[:4096]
        await append_aiq_event(
            session,
            agent_run_id=agent_run_id,
            event_kind="aiq.dispatch.failed",
            payload={"error": error[:1024]},
        )
        await session.commit()


async def aiq_submit_job(
    ctx: dict[str, Any],
    agent_run_id_str: str,
    project_id_str: str,
    topic: str,
    depth: str,
    enabled_connectors: list[str],
    agent_token: str,
    attempt: int = 0,
) -> dict[str, Any]:
    secret: str = ctx["agent_token_secret"]
    claims = verify_agent_token(agent_token, secret=secret)
    agent_run_id = UUID(agent_run_id_str)
    project_id = UUID(project_id_str)
    maker = ctx["session_maker"]
    settings = ctx["settings"]

    with start_span(
        "worker.aiq_submit",
        **{
            "aleph.project_id": project_id_str,
            "aleph.agent_run_id": agent_run_id_str,
            "aleph.attempt": attempt,
        },
    ):
        try:
            outcome: SubmitOutcome = await submit_core(
                settings=settings,
                redis_pool=ctx["redis_pool"],
                project_id=project_id,
                principal_user_id=claims.user_id,
                agent_run_id=agent_run_id,
                topic=topic,
                depth=depth,
                enabled_connectors=enabled_connectors,
                poll_agent_token=agent_token,
                attempt=attempt,
            )
        except Exception as exc:
            await _mark_failed(maker, agent_run_id, str(exc))
            raise

        if outcome.status == "requeued":
            return {"status": "queued", "attempt": attempt}
        if outcome.status == "exhausted":
            await _mark_failed(
                maker,
                agent_run_id,
                f"AIQ submission queue timeout after {attempt + 1} attempts",
            )
            return {"status": "failed", "reason": "queue_timeout"}

        async with maker() as session:
            await append_aiq_event(
                session,
                agent_run_id=agent_run_id,
                event_kind="aiq.job.dispatched",
                payload={"aiq_job_id": outcome.aiq_job_id, "queued_attempts": attempt},
            )
            await session.commit()
        return {"status": "dispatched", "aiq_job_id": outcome.aiq_job_id}
