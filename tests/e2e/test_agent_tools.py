"""Integration test for the agent-facing source ingestion route.

Covers `POST /v1/projects/{project_id}/sources/ingest-url` — the route the
`ingest_source` agent tool self-calls. Reuses the shared `http_client` +
`auth_bypass` fixtures from conftest and creates a project inline (there is
no standalone project_id fixture in this suite).

This hits a real outbound URL (https://example.com/) so it requires network
egress in addition to the compose stack.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _create_project(http_client) -> str:
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Agent-tool ledger test", "description": "", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    return proj.json()["id"]


async def _ledger_kinds(asgi_app, project_id: str) -> list[str]:
    """Return the action_kind of every ActionLedgerEvent for a project.

    Mirrors the DB-access pattern in test_project_lifecycle.py: query the
    real rows through the app's shared session_maker — no mocks.
    """
    from aleph_db.models.ledger import ActionLedgerEvent

    maker = asgi_app.state.session_maker
    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(ActionLedgerEvent)
                    .where(ActionLedgerEvent.project_id == project_id)
                    .order_by(ActionLedgerEvent.timestamp)
                )
            )
            .scalars()
            .all()
        )
    return [r.action_kind for r in rows]


@pytest.fixture
async def auth_bypass(monkeypatch):
    """Replace JWT verification with a fixed claim set so we don't need an IdP."""
    from aleph_security import jwt as jwt_module

    fixed_subject = "test-user-agent-tools"
    fixed_email = "agent-tools@test.local"

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": fixed_subject, "email": fixed_email, "name": "Agent Tools Tester"}

    monkeypatch.setattr(jwt_module, "verify_user_jwt", fake_verify)
    from aleph_api.middleware import auth as auth_mod

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)
    yield {"subject": fixed_subject, "email": fixed_email}


async def test_ingest_url_creates_source(http_client, auth_bypass):
    """Ingesting a URL fetches it server-side and registers a Source."""
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Ingest URL test", "description": "", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    project_id = proj.json()["id"]

    resp = await http_client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={"url": "https://example.com/", "title": "Example"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in ("normalizing", "pending")
    assert body["source_id"]


async def test_ingest_url_writes_source_ledger(http_client, auth_bypass, asgi_app):
    """The ingest-url route (mirrored by the `ingest_source` agent tool) writes
    the source-creation ledger events (rule #4). `register_uploaded_source`
    emits both `source.create` and `source_version.create`."""
    project_id = await _create_project(http_client)

    resp = await http_client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={"url": "https://example.com/", "title": "Example"},
    )
    assert resp.status_code == 201, resp.text

    kinds = await _ledger_kinds(asgi_app, project_id)
    assert "source.create" in kinds, kinds
    assert "source_version.create" in kinds, kinds


async def test_create_hypothesis_writes_ledger(http_client, auth_bypass, asgi_app):
    """Creating a hypothesis via the route the `create_hypothesis_tool` mirrors
    writes a `hypothesis.create` ledger event (rule #4)."""
    project_id = await _create_project(http_client)

    resp = await http_client.post(
        f"/v1/projects/{project_id}/hypotheses",
        json={
            "title": "Test hypothesis",
            "statement": "The ledger records hypothesis creation.",
        },
    )
    assert resp.status_code == 201, resp.text

    kinds = await _ledger_kinds(asgi_app, project_id)
    assert "hypothesis.create" in kinds, kinds
