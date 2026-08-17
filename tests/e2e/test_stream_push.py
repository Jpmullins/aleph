"""Integration tests: the migrated SSE streams wake on push (not just fallback).

httpx ASGITransport does not stream (it runs the app to completion), so an
infinite SSE generator can't be driven through the test client. We call the route
handler directly and pump its `StreamingResponse.body_iterator` into a queue via a
background task — exercising the real subscribe → wait → requery → emit logic
against the live ChangeBroker + NotifyListener built by the lifespan.

Why a pump task (and not `wait_for(anext(gen))` directly): timing out a
`wait_for(anext(agen))` cancels the in-flight `__anext__`, which injects the
cancellation INTO the generator and destroys it. Reading from a queue fed by a
background pump keeps the generator alive across read timeouts.

Each test reads the first heartbeat (proving the broker subscription is live),
commits a write, then asserts the event surfaces well within the stream's fallback
window — i.e. via LISTEN/NOTIFY push. Companion tests stop the listener (fallback
self-heals) and check project scoping.

Requires Postgres (+ the realtime triggers) + Redis; the e2e fixtures drive the
lifespan so the listener is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentEvent, AgentRun

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-stream", "email": "stream@test.local", "name": "Stream"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _scoped_project(http_client: Any, asgi_app: Any) -> tuple[UUID, Any]:
    """A real project + an owner Principal.

    The stream routes run `assert_stream_access` (membership check with a
    short-lived session) before streaming, so a bare random project id 404s.
    """
    from aleph_db.models.project import Project
    from aleph_security.principal import Principal

    resp = await http_client.post("/v1/projects", json={"title": "stream-push", "description": ""})
    assert resp.status_code == 201, resp.text
    pid = UUID(resp.json()["id"])
    maker = asgi_app.state.session_maker
    async with maker() as session:
        project = await session.get(Project, pid)
        assert project is not None
        owner_id = project.created_by
    principal = Principal(
        user_id=owner_id, subject="user-stream", email="stream@test.local", actor_kind="user"
    )
    return pid, principal


def _fake_request(asgi_app: Any) -> Any:
    from types import SimpleNamespace

    async def is_disconnected() -> bool:
        return False

    # `query_params` / `headers` are read by the surface stream for the WP-4
    # reconnect cursor (`?cid=`, `Last-Event-ID`); real Starlette requests
    # always carry them, so the fake supplies empty stand-ins.
    return SimpleNamespace(
        # `assert_stream_access` consults project status, which needs
        # method + path. Streams are always GET.
        method="GET",
        url=SimpleNamespace(path="/v1/projects/stream"),
        app=asgi_app,
        is_disconnected=is_disconnected,
        query_params={},
        headers={},
    )


class _StreamReader:
    """Pump a StreamingResponse's body into a queue via a background task.

    Reads time out by cancelling `queue.get` — never the generator — so the
    stream survives across read deadlines.
    """

    _DONE = object()

    def __init__(self, response: Any) -> None:
        self._it = response.body_iterator
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for frame in self._it:
                await self._q.put(frame.decode() if isinstance(frame, bytes) else frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface a generator error to the reader
            await self._q.put(exc)
        finally:
            await self._q.put(self._DONE)

    async def _next_frame(self, *, deadline: float) -> str:
        item = await asyncio.wait_for(self._q.get(), timeout=deadline)
        if item is self._DONE:
            raise AssertionError("stream ended unexpectedly")
        if isinstance(item, Exception):
            raise item
        return item

    async def wait_heartbeat(self, *, deadline: float = 5.0) -> None:
        loop = asyncio.get_event_loop()
        end = loop.time() + deadline
        while loop.time() < end:
            if "heartbeat" in await self._next_frame(deadline=deadline):
                return
        raise AssertionError("no heartbeat within deadline")

    async def next_json(self, *, deadline: float) -> dict[str, Any]:
        loop = asyncio.get_event_loop()
        end = loop.time() + deadline
        while loop.time() < end:
            for line in (await self._next_frame(deadline=deadline)).splitlines():
                if line.startswith("data:"):
                    body = line[len("data:") :].strip()
                    if body:
                        return json.loads(body)
        raise AssertionError("no data frame within deadline")

    async def settle(self, *, quiet: float = 1.5) -> None:
        """Drain frames until none arrive for `quiet` seconds (initial burst done)."""
        while True:
            try:
                item = await asyncio.wait_for(self._q.get(), timeout=quiet)
            except TimeoutError:
                return
            if item is self._DONE:
                return

    async def expect_no_data(self, *, window: float) -> None:
        """Assert no `data:` frame (heartbeats allowed) arrives within `window`."""
        loop = asyncio.get_event_loop()
        end = loop.time() + window
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                return
            try:
                item = await asyncio.wait_for(self._q.get(), timeout=remaining)
            except TimeoutError:
                return
            if item is self._DONE or isinstance(item, Exception):
                return
            for line in item.splitlines():
                if line.startswith("data:") and line[len("data:") :].strip():
                    raise AssertionError(f"unexpected data frame: {item!r}")

    async def aclose(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        with contextlib.suppress(Exception):
            await self._it.aclose()


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


async def test_agent_events_stream_wakes_on_push(http_client, auth_bypass, asgi_app):
    from aleph_api.routes.agent_events import stream_agent_events

    pid, principal = await _scoped_project(http_client, asgi_app)
    resp = await stream_agent_events(pid, _fake_request(asgi_app), principal, since=None)
    reader = _StreamReader(resp)
    try:
        await reader.wait_heartbeat()  # subscription is now live
        # Push should surface this in << 5s (the fallback window). 3s proves push.
        await _write_agent_event(asgi_app, project_id=pid, event_kind="phase_started")
        body = await reader.next_json(deadline=3.0)
        assert body["event_kind"] == "phase_started"
        assert body["agent_kind"] == "wiki"
    finally:
        await reader.aclose()


async def test_agent_events_stream_self_heals_via_fallback(http_client, auth_bypass, asgi_app):
    from aleph_api.routes.agent_events import stream_agent_events

    pid, principal = await _scoped_project(http_client, asgi_app)
    # With the listener stopped (no push), a write must still surface via the
    # fallback poll. Fallback is 5s, so allow 8s.
    await asgi_app.state.notify_listener.stop()
    try:
        resp = await stream_agent_events(pid, _fake_request(asgi_app), principal, since=None)
        reader = _StreamReader(resp)
        try:
            await reader.wait_heartbeat()
            await _write_agent_event(asgi_app, project_id=pid, event_kind="phase_completed")
            body = await reader.next_json(deadline=8.0)
            assert body["event_kind"] == "phase_completed"
        finally:
            await reader.aclose()
    finally:
        await asgi_app.state.notify_listener.start()


async def test_agent_events_stream_is_project_scoped(http_client, auth_bypass, asgi_app):
    from aleph_api.routes.agent_events import stream_agent_events

    pid, principal = await _scoped_project(http_client, asgi_app)
    other = uuid4()
    resp = await stream_agent_events(pid, _fake_request(asgi_app), principal, since=None)
    reader = _StreamReader(resp)
    try:
        await reader.wait_heartbeat()
        # A write for a DIFFERENT project must not surface on this stream.
        await _write_agent_event(asgi_app, project_id=other, event_kind="phase_started")
        await reader.expect_no_data(window=2.0)
    finally:
        await reader.aclose()


async def test_surfaces_stream_emits_initial_surface_then_idles(http_client, auth_bypass, asgi_app):
    """The surface delta-stream emits the full v0.9 surface on connect.

    WP-4 made the canonical tabs data-bound, so the surface stream now also emits
    per-mutation `updateDataModel` deltas (covered by
    `test_surface_stream_resume.py`). This test just asserts the initial
    full-snapshot handshake — the first frame is the v0.9 `createSurface`.
    """
    from aleph_api.routes.surfaces import stream_surface

    pid, principal = await _scoped_project(http_client, asgi_app)

    resp = await stream_surface(pid, "notes", _fake_request(asgi_app), principal, page_id=None)
    reader = _StreamReader(resp)
    try:
        first = await reader.next_json(deadline=5.0)
        assert first.get("version") == "v0.9"
        assert "createSurface" in first or "updateComponents" in first
    finally:
        await reader.aclose()


async def _write_compile_event(
    asgi_app: Any, *, project_id: UUID, event_kind: str, payload: dict[str, Any]
) -> None:
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
                id=uuid7(), agent_run_id=run_id, event_kind=event_kind, payload_jsonb=payload
            )
        )
        await session.commit()


async def test_changes_stream_emits_committed_on_wiki_commit(http_client, auth_bypass, asgi_app):
    from aleph_api.routes.changes import stream_changes
    from aleph_core.time import utcnow
    from aleph_db.models.ledger import ActionLedgerEvent

    pid, principal = await _scoped_project(http_client, asgi_app)
    page_id = uuid7()
    resp = await stream_changes(pid, _fake_request(asgi_app), principal, since=None)
    reader = _StreamReader(resp)
    try:
        await reader.settle()  # consume the initial (replay + heartbeat) burst
        maker = asgi_app.state.session_maker
        async with maker() as session:
            session.add(
                ActionLedgerEvent(
                    id=uuid7(),
                    project_id=pid,
                    actor_id=uuid7(),
                    actor_kind="aleph_agent",
                    action_kind="wiki.revision.commit",
                    target_id=page_id,
                    target_kind="wiki_page",
                    payload_jsonb={"page_title": "Acme", "page_id": str(page_id)},
                    timestamp=utcnow(),
                    chain_hash="0" * 64,
                )
            )
            await session.commit()
        sig = await reader.next_json(deadline=3.0)
        assert sig["kind"] == "committed"
        assert sig["page_title"] == "Acme"
        assert sig["page_id"] == str(page_id)
    finally:
        await reader.aclose()


async def test_changes_stream_emits_compiling_on_compile_page(http_client, auth_bypass, asgi_app):
    from aleph_api.routes.changes import stream_changes

    pid, principal = await _scoped_project(http_client, asgi_app)
    resp = await stream_changes(pid, _fake_request(asgi_app), principal, since=None)
    reader = _StreamReader(resp)
    try:
        await reader.settle()
        await _write_compile_event(
            asgi_app,
            project_id=pid,
            event_kind="phase_started",
            payload={"phase": "compile_page", "page_title": "Acme", "page_kind": "source"},
        )
        sig = await reader.next_json(deadline=3.0)
        assert sig["kind"] == "compiling"
        assert sig["page_title"] == "Acme"
    finally:
        await reader.aclose()
