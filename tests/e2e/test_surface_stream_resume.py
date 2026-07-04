"""WP-4 sub-spec (a) integration: the delta substrate is load-bearing.

These drive the LIVE surface SSE route (`routes.surfaces.stream_surface`), not
just the streamer unit, proving the reconnect/resume + ordering contract:

  (i)   resume after a dropped connection replays only the MISSED deltas
        (an `updateDataModel`, not a full resnapshot);
  (ii)  a gap beyond the ring (buffer gone) triggers a clean full-snapshot
        resync (createSurface + updateComponents + root updateDataModel);
  (iii) the server stamps a strictly-increasing `seq` on every message
        (the ordering the client applies + de-dupes against).

httpx ASGITransport does not stream an infinite SSE generator, so — as in
`test_stream_push.py` — we call the handler directly and pump its
`StreamingResponse.body_iterator` into a queue via a background task. The
reconnect forward-diff is emitted SYNCHRONOUSLY at connect (before the wait
loop), so the resume/gap assertions are deterministic and independent of
LISTEN/NOTIFY timing. The per-connection replay ring lives in a module-level
registry keyed by `?cid=`, so a second handler call with the same cid resumes
the first connection's buffer.

Requires Postgres (+ realtime triggers) + Redis; the e2e fixtures drive the
lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from itertools import pairwise
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-surface", "email": "surface@test.local", "name": "Surface"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _scoped_project(http_client: Any, asgi_app: Any) -> tuple[UUID, Any]:
    """A real project + its owner Principal (the stream route runs a membership
    check before streaming, so a random project id would 404)."""
    from aleph_db.models.project import Project
    from aleph_security.principal import Principal

    resp = await http_client.post(
        "/v1/projects", json={"title": "surface-resume", "description": "", "budget_usd": "1.00"}
    )
    assert resp.status_code == 201, resp.text
    pid = UUID(resp.json()["id"])
    async with asgi_app.state.session_maker() as session:
        project = await session.get(Project, pid)
        assert project is not None
        owner_id = project.created_by
    principal = Principal(
        user_id=owner_id, subject="user-surface", email="surface@test.local", actor_kind="user"
    )
    return pid, principal


def _fake_request(
    asgi_app: Any, *, cid: str | None = None, last_event_id: int | None = None
) -> Any:
    async def is_disconnected() -> bool:
        return False

    headers: dict[str, str] = {}
    if last_event_id is not None:
        headers["last-event-id"] = str(last_event_id)
    query: dict[str, str] = {}
    if cid is not None:
        query["cid"] = cid
    return SimpleNamespace(
        app=asgi_app, is_disconnected=is_disconnected, headers=headers, query_params=query
    )


class _StreamReader:
    """Pump a StreamingResponse's body into a queue (background task) so read
    timeouts never cancel the generator itself (see test_stream_push)."""

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
        except Exception as exc:  # surface generator errors to the reader
            await self._q.put(exc)
        finally:
            await self._q.put(self._DONE)

    async def next_json(self, *, deadline: float) -> dict[str, Any]:
        loop = asyncio.get_event_loop()
        end = loop.time() + deadline
        while loop.time() < end:
            item = await asyncio.wait_for(self._q.get(), timeout=max(0.01, end - loop.time()))
            if item is self._DONE:
                raise AssertionError("stream ended unexpectedly")
            if isinstance(item, Exception):
                raise item
            for line in item.splitlines():
                if line.startswith("data:"):
                    body = line[len("data:") :].strip()
                    if body:
                        return json.loads(body)
        raise AssertionError("no data frame within deadline")

    async def aclose(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        with contextlib.suppress(Exception):
            await self._it.aclose()


def _kind(msg: dict[str, Any]) -> str:
    for k in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface"):
        if k in msg:
            return k
    return "?"


async def _snapshot(handler, pid, tab, principal, asgi_app, *, cid) -> list[dict[str, Any]]:
    """Open a fresh connection and read the 3-message full snapshot."""
    resp = await handler(pid, tab, _fake_request(asgi_app, cid=cid), principal, page_id=None)
    reader = _StreamReader(resp)
    try:
        frames = [await reader.next_json(deadline=5.0) for _ in range(3)]
    finally:
        await reader.aclose()
    return frames


async def test_resume_applies_only_missed_deltas(http_client, auth_bypass, asgi_app, monkeypatch):
    from aleph_api.routes.surfaces import stream_surface

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid, principal = await _scoped_project(http_client, asgi_app)
    cid = f"resume-{pid}"

    # 1) Fresh connect → full snapshot (createSurface, updateComponents, root DM).
    snap = await _snapshot(stream_surface, pid, "hypotheses", principal, asgi_app, cid=cid)
    assert [_kind(m) for m in snap] == ["createSurface", "updateComponents", "updateDataModel"]
    last_seq = snap[-1]["seq"]
    assert snap[-1]["updateDataModel"]["value"]["items"] == []  # starts empty

    # 2) Mutate while "disconnected": create a hypothesis.
    created = await http_client.post(
        f"/v1/projects/{pid}/hypotheses",
        json={"title": "Resumed", "statement": "A falsifiable claim about resume."},
    )
    assert created.status_code == 201, created.text

    # 3) Reconnect with Last-Event-ID → only the missed delta, NO resnapshot.
    resp = await stream_surface(
        pid,
        "hypotheses",
        _fake_request(asgi_app, cid=cid, last_event_id=last_seq),
        principal,
        page_id=None,
    )
    reader = _StreamReader(resp)
    try:
        first = await reader.next_json(deadline=5.0)
    finally:
        await reader.aclose()
    assert _kind(first) == "updateDataModel", f"resume must not resnapshot, got {_kind(first)}"
    assert first["updateDataModel"]["path"].startswith("/items")
    assert first["seq"] > last_seq  # a NEW, higher seq


async def test_gap_beyond_buffer_triggers_full_resync(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_api.routes.surfaces import stream_surface

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid, principal = await _scoped_project(http_client, asgi_app)

    # A cid never seen + a Last-Event-ID the server can't place → the ring is
    # gone, so the server sends a clean full snapshot with a fresh baseline seq.
    resp = await stream_surface(
        pid,
        "notes",
        _fake_request(asgi_app, cid=f"gap-{pid}", last_event_id=999999),
        principal,
        page_id=None,
    )
    reader = _StreamReader(resp)
    try:
        frames = [await reader.next_json(deadline=5.0) for _ in range(3)]
    finally:
        await reader.aclose()
    assert [_kind(m) for m in frames] == ["createSurface", "updateComponents", "updateDataModel"]
    assert [m["seq"] for m in frames] == [0, 1, 2]  # fresh, contiguous baseline


async def test_server_stamps_strictly_increasing_seq(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_api.routes.surfaces import stream_surface

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid, principal = await _scoped_project(http_client, asgi_app)
    cid = f"seq-{pid}"

    snap = await _snapshot(stream_surface, pid, "hypotheses", principal, asgi_app, cid=cid)
    seqs = [m["seq"] for m in snap]
    assert seqs == [0, 1, 2]  # snapshot contiguous from the baseline

    created = await http_client.post(
        f"/v1/projects/{pid}/hypotheses",
        json={"title": "Seq", "statement": "Ordering is monotonic across a delta."},
    )
    assert created.status_code == 201, created.text

    resp = await stream_surface(
        pid,
        "hypotheses",
        _fake_request(asgi_app, cid=cid, last_event_id=seqs[-1]),
        principal,
        page_id=None,
    )
    reader = _StreamReader(resp)
    try:
        delta = await reader.next_json(deadline=5.0)
    finally:
        await reader.aclose()
    all_seqs = [*seqs, delta["seq"]]
    assert all(a < b for a, b in pairwise(all_seqs))  # strictly increasing


async def test_resume_cid_cannot_cross_projects(http_client, auth_bypass, asgi_app, monkeypatch):
    """A `cid` minted on project A's stream must NOT resume/replay project B's
    buffer (WP-4 security review Finding 2). The registry key is namespaced by
    (project_id, tab, cid), so the same raw cid on a different project is a
    distinct buffer → a fresh full snapshot, never A's buffered bytes."""
    from aleph_api.routes.surfaces import stream_surface

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid_a, principal_a = await _scoped_project(http_client, asgi_app)
    pid_b, principal_b = await _scoped_project(http_client, asgi_app)
    raw_cid = "shared-cid-abc"

    # Open A's stream with the cid so its buffer exists, and mutate A.
    await _snapshot(stream_surface, pid_a, "hypotheses", principal_a, asgi_app, cid=raw_cid)
    ca = await http_client.post(
        f"/v1/projects/{pid_a}/hypotheses",
        json={"title": "A-secret", "statement": "Only project A should see this."},
    )
    assert ca.status_code == 201, ca.text

    # B connects with the SAME raw cid + a Last-Event-ID that WOULD resume if the
    # buffer weren't project-scoped. It must get a fresh full snapshot for B,
    # containing none of A's items.
    resp = await stream_surface(
        pid_b,
        "hypotheses",
        _fake_request(asgi_app, cid=raw_cid, last_event_id=0),
        principal_b,
        page_id=None,
    )
    reader = _StreamReader(resp)
    try:
        frames = [await reader.next_json(deadline=5.0) for _ in range(3)]
    finally:
        await reader.aclose()
    # Full snapshot (not a bare replayed delta), and B's data model is empty —
    # A's "A-secret" hypothesis never leaks across the cid.
    assert [_kind(m) for m in frames] == ["createSurface", "updateComponents", "updateDataModel"]
    items = frames[-1]["updateDataModel"]["value"]["items"]
    assert all("A-secret" not in str(it) for it in items)
    assert items == []
