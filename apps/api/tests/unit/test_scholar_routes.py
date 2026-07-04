"""WP-2 scholar route unit tests — no DB, no lifespan, no network.

Chosen pattern (over e2e-integration): like `test_asset_stream_auth.py`,
the app is built without running the lifespan. The DB-backed seams are
swapped: `_principal_local_dev` is monkeypatched (the auth middleware
otherwise JIT-provisions a user row) and the route dependencies
(`session_dep`, `project_scope_dep`) are overridden with fakes — so the
routes' own logic (consensus binding enforcement, connector_kind
validation) runs for real against controlled rows.

The metadata-lands-on-the-Source-row case needs real Postgres, so it lives
in `tests/e2e/test_scholar_ingest.py` (`@pytest.mark.integration`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, Path

from aleph_api.deps import principal_dep, session_dep
from aleph_api.middleware.project_scope import project_scope_dep
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Answers `execute()` calls with a queue of scalar_one_or_none values."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)

    async def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._results.pop(0))

    async def commit(self) -> None:  # session_dep's override skips this, defensive
        return None


def _build_app(monkeypatch: pytest.MonkeyPatch, *, session_results: list[Any]) -> FastAPI:
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    # No lifespan: only the pieces the exercised code paths read.
    app.state.settings = SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret="unit-test-secret-0123456789abcdef0123456789abcdef",
        aleph_consensus_monthly_search_cap=200,
    )

    principal = Principal(
        user_id=uuid4(), subject="unit", email="unit@test.local", actor_kind="user"
    )

    async def _fake_local_dev(_request: Any) -> Principal:
        return principal

    # The local-mode principal path otherwise JIT-provisions a User row (DB).
    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)

    async def _fake_scope(
        project_id: Annotated[UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> UUID:
        p.cache_role(project_id, ProjectRole.OWNER.value)
        return project_id

    async def _fake_session():
        yield _FakeSession(session_results)

    app.dependency_overrides[project_scope_dep] = _fake_scope
    app.dependency_overrides[session_dep] = _fake_session
    return app


async def test_consensus_search_403_connector_disabled_via_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project binding with enabled=False beats enabled_by_default=True:
    the route must 403 with detail `connector_disabled` before any
    credential or upstream work happens."""
    connector = SimpleNamespace(id=uuid4(), kind="consensus", enabled_by_default=True)
    binding = SimpleNamespace(enabled=False)
    app = _build_app(monkeypatch, session_results=[connector, binding])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/v1/projects/{uuid4()}/scholar/consensus-search",
            json={"query": "does caffeine improve endurance?"},
        )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "connector_disabled"


async def test_consensus_search_403_when_no_binding_and_not_default_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = SimpleNamespace(id=uuid4(), kind="consensus", enabled_by_default=False)
    app = _build_app(monkeypatch, session_results=[connector, None])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/v1/projects/{uuid4()}/scholar/consensus-search",
            json={"query": "screening question"},
        )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "connector_disabled"


async def test_ingest_url_rejects_unknown_connector_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown connector_kind is a 422 BEFORE any URL fetch — provenance
    names must exist in the connectors table (WP-2 §5)."""
    app = _build_app(monkeypatch, session_results=[None])  # connector lookup misses

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/v1/projects/{uuid4()}/sources/ingest-url",
            json={
                "url": "https://example.com/paper.pdf",
                "title": "Paper",
                "connector_kind": "made_up_origin",
                "source_metadata": {"doi": "10.1234/x"},
            },
        )

    assert resp.status_code == 422, resp.text
    assert "unknown connector_kind" in resp.json()["detail"]
