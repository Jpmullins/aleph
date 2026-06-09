"""Shared 'dispatch one AIQ research job' helper.

Used by the ``/synthesize`` route and the ``bootstrap_project_job`` so both
follow the identical path: resolve enabled connectors → create an ``AgentRun``
→ dispatch to AIQ → enqueue the poll job that lands a draft wiki page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_aiq.auth_bridge import issue_service_token
from aleph_aiq.client import AIQClient
from aleph_aiq.job_service import append_aiq_event, create_aiq_agent_run
from aleph_db.repos.ledger import LedgerWriter
from aleph_rks.models import Connector, ConnectorBinding
from aleph_security.agent_token import mint_agent_token


@dataclass
class StartedResearch:
    agent_run_id: UUID
    correlation_id: str
    aiq_job_id: str | None
    dispatched: bool


async def _resolve_enabled_connectors(
    session: AsyncSession, project_id: UUID, allowed: list[str] | None
) -> list[str]:
    stmt = (
        select(Connector.kind)
        .join(ConnectorBinding, ConnectorBinding.connector_id == Connector.id)
        .where(
            ConnectorBinding.project_id == project_id,
            ConnectorBinding.enabled.is_(True),
        )
    )
    enabled = [k for (k,) in (await session.execute(stmt)).all()]
    if allowed:
        enabled = [k for k in enabled if k in allowed]
    return enabled


async def _dispatch_core(
    *,
    settings: Any,
    redis_pool: Any,
    project_id: UUID,
    principal_user_id: UUID,
    agent_run_id: UUID,
    correlation_id: str,
    topic: str,
    depth: str,
    enabled_connectors: list[str],
) -> StartedResearch:
    """Mint tokens, check AIQ health, dispatch, and enqueue the poll job.

    Pure of DB writes (the AgentRun + ledger are written by the caller), so it
    is unit-testable with a fake AIQ client + fake arq pool.
    """
    service_token = issue_service_token(
        secret=settings.aleph_agent_token_secret,
        project_id=project_id,
        agent_run_id=agent_run_id,
        principal_user_id=principal_user_id,
    )
    poll_agent_token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=principal_user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )
    aiq_base = getattr(settings, "aiq_base_url", None) or "http://aiq-server:8000"
    client = AIQClient(base_url=aiq_base, service_token=service_token)
    if not await client.health():
        return StartedResearch(agent_run_id, correlation_id, None, False)
    aiq_job_id = await client.dispatch_deep(
        project_id=project_id,
        topic=topic,
        allowed_data_sources=enabled_connectors,
        depth=depth,
    )
    await redis_pool.enqueue_job(
        "aiq_synthesis_poll_job",
        str(agent_run_id),
        aiq_job_id,
        str(project_id),
        topic,
        poll_agent_token,
    )
    return StartedResearch(agent_run_id, correlation_id, aiq_job_id, True)


async def dispatch_research(
    *,
    session: AsyncSession,
    settings: Any,
    redis_pool: Any,
    project_id: UUID,
    principal_user_id: UUID,
    actor_kind: str,
    ledger: LedgerWriter,
    topic: str,
    depth: str = "deep",
    allowed_connectors: list[str] | None = None,
) -> StartedResearch:
    """Resolve connectors, create the run + ledger event, dispatch, enqueue poll.

    Raises ``ValueError`` if no connectors are enabled for the project.
    """
    enabled = await _resolve_enabled_connectors(session, project_id, allowed_connectors)
    if not enabled:
        msg = "no connectors are enabled for this project"
        raise ValueError(msg)

    started = await create_aiq_agent_run(
        session,
        project_id=project_id,
        topic=topic,
        depth=depth,
        allowed_connector_kinds=enabled,
        created_by=principal_user_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal_user_id,
        actor_kind=actor_kind,
        action_kind="synthesize.dispatch",
        target_id=started.agent_run_id,
        target_kind="agent_run",
        payload={"topic": topic, "depth": depth, "allowed_connectors": enabled},
        trace_id=None,
    )
    try:
        result = await _dispatch_core(
            settings=settings,
            redis_pool=redis_pool,
            project_id=project_id,
            principal_user_id=principal_user_id,
            agent_run_id=started.agent_run_id,
            correlation_id=started.correlation_id,
            topic=topic,
            depth=depth,
            enabled_connectors=enabled,
        )
    except Exception as exc:
        await append_aiq_event(
            session,
            agent_run_id=started.agent_run_id,
            event_kind="aiq.dispatch.failed",
            payload={"error": str(exc)[:1024]},
        )
        return StartedResearch(started.agent_run_id, started.correlation_id, None, False)
    if result.dispatched:
        await append_aiq_event(
            session,
            agent_run_id=started.agent_run_id,
            event_kind="aiq.job.dispatched",
            payload={"aiq_job_id": result.aiq_job_id},
        )
    return result
