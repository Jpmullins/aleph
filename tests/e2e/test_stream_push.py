"""Integration tests: the migrated SSE streams wake on push (not just fallback).

httpx ASGITransport does not stream (it runs the app to completion), so an
infinite SSE generator can't be driven through the test client. We instead call
the route handler directly and drive its `StreamingResponse.body_iterator`,
exercising the real subscribe → wait → requery → emit logic against the live
ChangeBroker + NotifyListener built by the lifespan.

Each test reads the first heartbeat frame (proving the broker subscription is
live), commits a write, then asserts the event surfaces well within the stream's
fallback window — i.e. via LISTEN/NOTIFY push. A companion test stops the listener
and asserts the write still surfaces via the fallback poll (self-healing).

Requires Postgres (+ the realtime triggers) + Redis; the e2e fixtures drive the
lifespan so the listener is running.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentEvent, AgentRun

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _fake_request(asgi_app: Any, *, disconnected: bool = False) -> Any:
    """A minimal stand-in for starlette Request exposing what the stream uses."""

    async def is_disconnected() -> bool:
        return disconnected

    return SimpleNamespace(app=asgi_app, is_disconnected=is_disconnected)


async def _write_agent_event(asgi_app: Any, *, project_id: UUID, event_kind: str) -> None:
    maker = asgi_app.state.session_maker
    run_id = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=run_id,
                project_id=project_id,
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


async def _next_frame(body_iter: Any, *, deadline: float) -> str:
    return (await asyncio.wait_for(anext(body_iter), timeout=deadline)).decode()


async def _read_until_heartbeat(body_iter: Any, *, deadline: float) -> None:
    loop = asyncio.get_event_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        frame = await _next_frame(body_iter, deadline=deadline)
        if "heartbeat" in frame:
            return
    raise AssertionError("no heartbeat within deadline")


async def _read_data_json(body_iter: Any, *, deadline: float) -> dict[str, Any]:
    """Return the first frame carrying a non-empty `data: {json}` payload."""
    loop = asyncio.get_event_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        frame = await _next_frame(body_iter, deadline=deadline)
        for line in frame.splitlines():
            if line.startswith("data:"):
                body = line[len("data:") :].strip()
                if body:
                    return json.loads(body)
    raise AssertionError("no data frame within deadline")


async def test_agent_events_stream_wakes_on_push(asgi_app):
    from aleph_api.routes.agent_events import stream_agent_events

    pid = uuid7()
    resp = await stream_agent_events(pid, _fake_request(asgi_app), since=None)
    body_iter = resp.body_iterator
    try:
        # First frame is the heartbeat → the broker subscription is now live.
        await _read_until_heartbeat(body_iter, deadline=5.0)
        # Write after subscribing; push should surface it in << 5s (the fallback
        # window). A 3s deadline proves it came via push, not the fallback poll.
        await _write_agent_event(asgi_app, project_id=pid, event_kind="phase_started")
        body = await _read_data_json(body_iter, deadline=3.0)
        assert body["event_kind"] == "phase_started"
        assert body["agent_kind"] == "wiki"
    finally:
        await body_iter.aclose()


async def test_agent_events_stream_self_heals_via_fallback(asgi_app):
    from aleph_api.routes.agent_events import stream_agent_events

    # With the listener stopped (no push), a write must still surface via the
    # fallback poll. Fallback is 5s, so allow 8s.
    await asgi_app.state.notify_listener.stop()
    try:
        pid = uuid7()
        resp = await stream_agent_events(pid, _fake_request(asgi_app), since=None)
        body_iter = resp.body_iterator
        try:
            await _read_until_heartbeat(body_iter, deadline=5.0)
            await _write_agent_event(asgi_app, project_id=pid, event_kind="phase_completed")
            body = await _read_data_json(body_iter, deadline=8.0)
            assert body["event_kind"] == "phase_completed"
        finally:
            await body_iter.aclose()
    finally:
        await asgi_app.state.notify_listener.start()


async def test_agent_events_stream_is_project_scoped(asgi_app):
    from aleph_api.routes.agent_events import stream_agent_events

    pid = uuid7()
    other = uuid4()
    resp = await stream_agent_events(pid, _fake_request(asgi_app), since=None)
    body_iter = resp.body_iterator
    try:
        await _read_until_heartbeat(body_iter, deadline=5.0)
        # A write for a DIFFERENT project must not surface on this stream. With
        # no signal for `pid`, no data frame arrives within the deadline — which
        # manifests as a timeout on the next frame (heartbeat is 5s out) or, if a
        # heartbeat lands, as "no data frame". Either proves the scoping holds.
        await _write_agent_event(asgi_app, project_id=other, event_kind="phase_started")
        with pytest.raises((TimeoutError, AssertionError)):
            await _read_data_json(body_iter, deadline=2.0)
    finally:
        await body_iter.aclose()
