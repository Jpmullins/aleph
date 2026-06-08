"""Creating a project enqueues the bootstrap job + records a bootstrap AgentRun."""

from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-boot-create", "email": "bc@test.local", "name": "BC"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_create_project_enqueues_bootstrap(http_client, auth_bypass, asgi_app, monkeypatch):
    # Capture enqueue_job calls instead of needing a live worker.
    calls: list[tuple[object, ...]] = []

    class _Pool:
        async def enqueue_job(self, *a: object, **k: object) -> None:
            calls.append(a)

        async def aclose(self) -> None: ...

    async def _fake_create_pool(*a: object, **k: object) -> _Pool:
        return _Pool()

    import aleph_api.routes.projects as proj

    monkeypatch.setattr(proj, "create_pool", _fake_create_pool)

    resp = await http_client.post(
        "/v1/projects", json={"title": "Quantum radar survey", "description": "OSINT scan"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    from aleph_db.models.agent import AgentRun

    async with asgi_app.state.session_maker() as session:
        runs = list(
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.project_id == project_id,
                        AgentRun.agent_kind == "bootstrap",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(runs) == 1
    assert any(c and c[0] == "bootstrap_project_job" for c in calls)


async def test_create_project_bootstrap_disabled(http_client, auth_bypass, asgi_app, monkeypatch):
    # With the flag off, no bootstrap run and no enqueue. monkeypatch.setattr
    # auto-restores after the test (Settings is lru_cached + shared across tests).
    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    calls: list[object] = []

    class _Pool:
        async def enqueue_job(self, *a: object, **k: object) -> None:
            calls.append(a)

        async def aclose(self) -> None: ...

    import aleph_api.routes.projects as proj

    monkeypatch.setattr(proj, "create_pool", lambda *a, **k: _Pool())  # type: ignore[arg-type]

    resp = await http_client.post(
        "/v1/projects", json={"title": "No bootstrap", "description": ""}
    )
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    from aleph_db.models.agent import AgentRun

    async with asgi_app.state.session_maker() as session:
        runs = list(
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.project_id == project_id,
                        AgentRun.agent_kind == "bootstrap",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert runs == []
    assert calls == []
