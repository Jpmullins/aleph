"""AIQ job → Aleph AgentRun lifecycle.

A `/synthesize` request creates an `AgentRun(agent_kind="aiq_deep")`,
issues a service token bound to that run, dispatches the AIQ job, and
records progress as `AgentEvent` rows via the callback (or via
direct service calls when running in-process for tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentEvent, AgentRun
from aleph_observability.tracing import current_trace_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class StartedAIQJob:
    agent_run_id: UUID
    correlation_id: str


async def create_aiq_agent_run(
    session: "AsyncSession",
    *,
    project_id: UUID,
    topic: str,
    depth: str,
    allowed_connector_kinds: list[str],
    created_by: UUID,
) -> StartedAIQJob:
    agent_run_id = uuid7()
    correlation_id = f"aiq-{agent_run_id.hex[:8]}"
    run = AgentRun(
        id=agent_run_id,
        project_id=project_id,
        agent_kind=f"aiq_{depth}",
        correlation_id=correlation_id,
        status="pending",
        input_payload={
            "topic": topic,
            "depth": depth,
            "allowed_connectors": allowed_connector_kinds,
        },
        created_by=created_by,
        access_scope="project",
    )
    session.add(run)
    await session.flush()
    return StartedAIQJob(agent_run_id=agent_run_id, correlation_id=correlation_id)


async def append_aiq_event(
    session: "AsyncSession",
    *,
    agent_run_id: UUID,
    event_kind: str,
    payload: dict,
) -> None:
    session.add(
        AgentEvent(
            id=uuid7(),
            agent_run_id=agent_run_id,
            event_kind=event_kind,
            payload_jsonb=payload,
            timestamp=utcnow(),
        )
    )
    await session.flush()
