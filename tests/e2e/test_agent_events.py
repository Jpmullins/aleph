"""Integration test for the agent-events surface (Wave 3 Activity card feed).

Requires Postgres + Redis + migrations (the standard e2e fixtures). Covers the
non-streaming `GET /v1/projects/{id}/agent-events` endpoint — the same query +
serialization the SSE `/stream` endpoint runs per poll — plus project scoping:
events from one project never leak into another's feed.
"""

from __future__ import annotations

import pytest

from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentEvent, AgentRun

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _create_project(http_client, title: str) -> str:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": title, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_run_with_event(
    asgi_app,
    *,
    project_id: str,
    agent_kind: str,
    correlation_id: str,
    event_kind: str,
    phase: str,
    duration_ms: int,
) -> tuple[str, str]:
    """Insert one AgentRun + one AgentEvent directly; return (run_id, event_id)."""
    from uuid import UUID

    maker = asgi_app.state.session_maker
    run_id = uuid7()
    event_id = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=run_id,
                project_id=UUID(project_id),
                agent_kind=agent_kind,
                correlation_id=correlation_id,
                status="running",
                created_by=uuid7(),
            )
        )
        await session.flush()
        session.add(
            AgentEvent(
                id=event_id,
                agent_run_id=run_id,
                event_kind=event_kind,
                payload_jsonb={"phase": phase, "duration_ms": duration_ms, "note": "hi"},
            )
        )
        await session.commit()
    return str(run_id), str(event_id)


async def test_list_agent_events_shape(http_client, asgi_app):
    pid = await _create_project(http_client, "events-shape")
    run_id, event_id = await _seed_run_with_event(
        asgi_app,
        project_id=pid,
        agent_kind="wiki",
        correlation_id=f"corr-{uuid7()}",
        event_kind="phase_completed",
        phase="concept_extraction",
        duration_ms=123,
    )

    resp = await http_client.get(f"/v1/projects/{pid}/agent-events")
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    ev = events[0]
    assert ev["id"] == event_id
    assert ev["agent_run_id"] == run_id
    assert ev["agent_kind"] == "wiki"
    assert ev["event_kind"] == "phase_completed"
    # phase + duration_ms are lifted out of payload; the rest stays under payload.
    assert ev["phase"] == "concept_extraction"
    assert ev["duration_ms"] == 123
    assert ev["payload"] == {"note": "hi"}
    assert "phase" not in ev["payload"] and "duration_ms" not in ev["payload"]


async def test_agent_events_are_project_scoped(http_client, asgi_app):
    pid_a = await _create_project(http_client, "events-A")
    pid_b = await _create_project(http_client, "events-B")
    await _seed_run_with_event(
        asgi_app,
        project_id=pid_a,
        agent_kind="wiki",
        correlation_id=f"corr-a-{uuid7()}",
        event_kind="phase_started",
        phase="ingest",
        duration_ms=0,
    )

    # Project B's feed must not contain project A's event.
    resp_b = await http_client.get(f"/v1/projects/{pid_b}/agent-events")
    assert resp_b.status_code == 200, resp_b.text
    assert resp_b.json() == []

    # Project A's feed has exactly the one event.
    resp_a = await http_client.get(f"/v1/projects/{pid_a}/agent-events")
    assert resp_a.status_code == 200
    assert len(resp_a.json()) == 1
