"""SSE stream of AgentEvent rows for the right-panel Activity card.

Frontend opens an `EventSource` on this endpoint and receives a server-
sent event per new `AgentEvent` row scoped to the project's agent runs.
The activity card uses these to render per-phase progress (which the
top-level `AgentRun.status` cannot express).

Implementation: cursor-based polling against Postgres. Cheap and
infrastructure-free. Upgrade to `LISTEN/NOTIFY` if 500 ms latency
becomes a problem.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select
from starlette.responses import StreamingResponse

from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_db.models.agent import AgentEvent, AgentRun

router = APIRouter(prefix="/v1/projects", tags=["agent-events"])

_POLL_INTERVAL_SEC = 0.75
_BATCH_LIMIT = 100


@router.get("/{project_id}/agent-events")
async def list_agent_events(
    project_id: ProjectScopeDep,
    request: Request,
    since: Annotated[datetime | None, Query()] = None,
    agent_run_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[dict]:
    """Non-streaming list of recent AgentEvent rows for a project.

    Useful for the test suite and for initial activity-card hydration
    before the SSE stream is established.
    """
    maker = request.app.state.session_maker
    async with maker() as session:
        stmt = (
            select(AgentEvent, AgentRun.agent_kind)
            .join(AgentRun, AgentRun.id == AgentEvent.agent_run_id)
            .where(AgentRun.project_id == project_id)
        )
        if since is not None:
            stmt = stmt.where(AgentEvent.timestamp > since)
        if agent_run_id is not None:
            from uuid import UUID

            stmt = stmt.where(AgentEvent.agent_run_id == UUID(agent_run_id))
        stmt = stmt.order_by(AgentEvent.timestamp.desc()).limit(limit)
        rows = (await session.execute(stmt)).all()

    out: list[dict] = []
    for event, agent_kind in rows:
        payload = event.payload_jsonb or {}
        out.append(
            {
                "id": str(event.id),
                "agent_run_id": str(event.agent_run_id),
                "agent_kind": agent_kind,
                "event_kind": event.event_kind,
                "phase": payload.get("phase"),
                "duration_ms": payload.get("duration_ms"),
                "payload": {k: v for k, v in payload.items() if k not in ("phase", "duration_ms")},
                "timestamp": event.timestamp.isoformat(),
            }
        )
    return out


@router.get("/{project_id}/agent-events/stream")
async def stream_agent_events(
    project_id: ProjectScopeDep,
    request: Request,
    since: Annotated[datetime | None, Query()] = None,
):
    """SSE stream of AgentEvent rows for the project's agent runs.

    `since` is an optional cursor (ISO 8601 timestamp). The stream emits
    events with `timestamp > since`, in ascending timestamp order. Each
    SSE `data:` is JSON with shape:

        {
          "id": "<uuid>",
          "agent_run_id": "<uuid>",
          "agent_kind": "wiki" | "chunk_embed" | ...,
          "event_kind": "phase_started" | "phase_completed" | "phase_failed",
          "phase": "concept_extraction" | ...,
          "duration_ms": int | null,
          "payload": {...},
          "timestamp": "2026-05-28T14:32:00+00:00"
        }
    """
    maker = request.app.state.session_maker
    cursor: datetime = since or datetime.now(tz=UTC)

    async def _gen() -> AsyncIterator[bytes]:
        nonlocal cursor
        while True:
            if await request.is_disconnected():
                return

            async with maker() as session:
                stmt = (
                    select(AgentEvent, AgentRun.agent_kind)
                    .join(AgentRun, AgentRun.id == AgentEvent.agent_run_id)
                    .where(
                        AgentRun.project_id == project_id,
                        AgentEvent.timestamp > cursor,
                    )
                    .order_by(AgentEvent.timestamp.asc())
                    .limit(_BATCH_LIMIT)
                )
                rows = (await session.execute(stmt)).all()

            for event, agent_kind in rows:
                payload = event.payload_jsonb or {}
                body = {
                    "id": str(event.id),
                    "agent_run_id": str(event.agent_run_id),
                    "agent_kind": agent_kind,
                    "event_kind": event.event_kind,
                    "phase": payload.get("phase"),
                    "duration_ms": payload.get("duration_ms"),
                    "payload": {
                        k: v for k, v in payload.items() if k not in ("phase", "duration_ms")
                    },
                    "timestamp": event.timestamp.isoformat(),
                }
                yield f"event: phase\ndata: {json.dumps(body)}\n\n".encode()
                cursor = event.timestamp

            # Heartbeat so proxies don't close idle connections.
            yield b"event: heartbeat\ndata: \n\n"
            await asyncio.sleep(_POLL_INTERVAL_SEC)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
