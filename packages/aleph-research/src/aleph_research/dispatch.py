"""Create + enqueue one native research run.

Shared by the ``/synthesize`` route and the ``bootstrap_project_job``
fan-out so both follow the identical path: resolve enabled connector
kinds → create the ``AgentRun(status="pending")`` → ledger
``synthesize.dispatch`` in the same transaction → mint the agent token →
enqueue ``deep_research_job``. No HTTP callbacks, no polling, no slots.
"""

# pyright: reportMissingTypeStubs=false
# (first-party aleph_* packages ship inline types but no py.typed marker —
#  the same visible debt every other package in this repo carries)

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_research.tools import resolve_enabled_kinds
from aleph_security.agent_token import mint_agent_token

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter

DEPTH_AGENT_KINDS: dict[str, str] = {
    "deep": "deep_research",
    "shallow": "shallow_research",
}


@dataclass(frozen=True)
class StartedResearch:
    agent_run_id: UUID
    correlation_id: str
    dispatched: bool = True


async def dispatch_research(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    redis_pool: Any,
    agent_token_secret: str,
    project_id: UUID,
    principal_user_id: UUID,
    actor_kind: str,
    topic: str,
    depth: str = "deep",
    allowed_connectors: list[str] | None = None,
) -> StartedResearch:
    """Create the run, ledger the dispatch, and enqueue ``deep_research_job``.

    Raises ``ValueError`` if no connectors are enabled for the project.
    """
    enabled = await resolve_enabled_kinds(
        session, project_id=project_id, allowed=allowed_connectors
    )
    if not enabled:
        msg = "no connectors are enabled for this project"
        raise ValueError(msg)

    agent_kind = DEPTH_AGENT_KINDS.get(depth, "deep_research")
    agent_run_id = uuid7()
    # Full hex, not a prefix: uuid7's leading bits are a ms timestamp, so
    # truncation collides for runs created in the same window (e.g. the
    # bootstrap fan-out) against uq_agent_runs_correlation_id.
    correlation_id = f"research-{agent_run_id.hex}"
    run = AgentRun(
        id=agent_run_id,
        project_id=project_id,
        agent_kind=agent_kind,
        correlation_id=correlation_id,
        status="pending",
        input_payload={
            "topic": topic,
            "depth": depth,
            "allowed_connectors": enabled,
        },
        created_by=principal_user_id,
        access_scope="project",
    )
    session.add(run)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal_user_id,
        actor_kind=actor_kind,
        action_kind="synthesize.dispatch",
        target_id=agent_run_id,
        target_kind="agent_run",
        payload={"topic": topic, "depth": depth, "allowed_connectors": enabled},
        trace_id=None,
    )
    token = mint_agent_token(
        secret=agent_token_secret,
        user_id=principal_user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )
    # Commit the run + ledger BEFORE enqueue: the worker must never race an
    # uncommitted row (a not-found there is a plain exception arq would retry,
    # and — with the run absent — could never be marked failed → strand).
    # Committing first makes the row unconditionally visible when the job runs.
    await session.commit()
    try:
        await redis_pool.enqueue_job("deep_research_job", str(agent_run_id), token)
    except Exception:
        # Redis unreachable after the run committed: no job will ever run to
        # converge it. Mark the committed run failed here so it never strands
        # as `pending`, then re-raise for the caller.
        run.status = "failed"
        run.error_text = "research enqueue failed (redis unreachable)"
        run.completed_at = utcnow()
        await session.commit()
        raise
    return StartedResearch(agent_run_id=agent_run_id, correlation_id=correlation_id)
