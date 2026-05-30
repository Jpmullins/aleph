"""Integration test for the real-time push layer end-to-end.

Proves: a committed DB write fires the pg_notify trigger → the lifespan's
NotifyListener receives it → the ChangeBroker fans it out to a subscriber, scoped
to the right project. Requires Postgres (with the `realtime_notify_triggers`
migration applied) + Redis — the standard e2e fixtures, which also drive the
lifespan (so the listener is started).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentEvent, AgentRun

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _create_project(http_client, title: str) -> str:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": title, "description": "", "budget_usd": "1.00"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _write_agent_event(asgi_app, *, project_id: str, event_kind: str) -> None:
    """Insert an AgentRun + AgentEvent (committed) → fires the agent_events trigger."""
    maker = asgi_app.state.session_maker
    run_id = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=run_id,
                project_id=UUID(project_id),
                agent_kind="wiki",
                correlation_id=f"corr-{uuid7()}",
                status="running",
                created_by=uuid7(),
            )
        )
        await session.flush()
        session.add(
            AgentEvent(
                id=uuid7(),
                agent_run_id=run_id,
                event_kind=event_kind,
                payload_jsonb={"phase": "compile_page", "page_id": str(uuid7())},
            )
        )
        await session.commit()


async def test_push_delivers_to_subscriber(http_client, asgi_app):
    pid = await _create_project(http_client, "push-e2e")
    broker = asgi_app.state.change_broker

    # Subscribe BEFORE the write (the broker doesn't buffer for non-subscribers).
    async with broker.subscribe(UUID(pid)) as sub:
        await _write_agent_event(asgi_app, project_id=pid, event_kind="phase_started")
        # Generous timeout: listener round-trips through Postgres NOTIFY.
        signal = await sub.wait(timeout=5.0)

    assert signal is not None, "no push signal received within timeout"
    assert signal["src"] == "agent_event"
    assert signal["project_id"] == pid
    assert signal["event_kind"] == "phase_started"


async def test_push_is_project_scoped(http_client, asgi_app):
    pid_a = await _create_project(http_client, "push-A")
    pid_b = await _create_project(http_client, "push-B")
    broker = asgi_app.state.change_broker

    async with broker.subscribe(UUID(pid_b)) as sub_b:
        # A write for project A must not reach project B's subscriber.
        await _write_agent_event(asgi_app, project_id=pid_a, event_kind="phase_started")
        assert await sub_b.wait(timeout=1.0) is None


async def test_ledger_write_pushes_committed_signal(http_client, asgi_app):
    # Creating a project writes ledger events; subscribe first, then a fresh
    # ledger-writing mutation (a second project would be a different pid, so
    # instead trigger an agent_event which carries this project's id). To
    # exercise the LEDGER trigger specifically, post a hypothesis (writes a
    # hypothesis.* ledger event scoped to this project).
    pid = await _create_project(http_client, "push-ledger")
    broker = asgi_app.state.change_broker

    async with broker.subscribe(UUID(pid)) as sub:
        resp = await http_client.post(
            f"/v1/projects/{pid}/hypotheses",
            json={"title": "H-push", "statement": "s"},
        )
        assert resp.status_code in (200, 201), resp.text
        # Collect signals for a moment; at least one should be a ledger signal.
        first = await sub.wait(timeout=5.0)
        rest = sub.drain()

    signals = [s for s in [first, *rest] if s is not None]
    assert any(s.get("src") == "ledger" for s in signals), signals
