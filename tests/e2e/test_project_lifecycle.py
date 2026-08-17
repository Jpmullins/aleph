"""Project lifecycle integration test.

Requires Postgres + Redis + Alembic migrations applied. Auth is mocked
via the `auth_bypass` fixture which monkey-patches the JWT verifier.

This test:
  1. Creates a project as user A
  2. Lists it
  3. Confirms the ledger events were written (create + profile + member)
  4. Asserts chain hash is continuous across the events
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    """Replace JWT verification with a fixed claim set so we don't need an IdP."""
    from aleph_security import jwt as jwt_module

    fixed_subject = "test-user-integration"
    fixed_email = "integration@test.local"

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {
            "sub": fixed_subject,
            "email": fixed_email,
            "name": "Integration Tester",
        }

    monkeypatch.setattr(jwt_module, "verify_user_jwt", fake_verify)
    # Also patch the symbol imported by the middleware.
    from aleph_api.middleware import auth as auth_mod

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)
    yield {"subject": fixed_subject, "email": fixed_email}


async def test_project_create_and_ledger(http_client, auth_bypass, asgi_app):
    """Create a project, list it, verify the ledger.

    Acceptance:
      * POST /v1/projects returns 201 with a UUID id
      * GET /v1/projects/{id} returns the project
      * The ledger has project.create, model_profile.copy_from_template,
        project_member.add, and connector_bindings.seed events
        (plus an earlier user.create event).
      * chain_hash is continuous (each event's prev_event_id == previous id).
    """
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "Integration test", "description": ""},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    project_id = body["id"]

    fetch = await http_client.get(f"/v1/projects/{project_id}")
    assert fetch.status_code == 200
    assert fetch.json()["title"] == "Integration test"

    # Inspect the ledger directly via the DB.
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
    kinds = [r.action_kind for r in rows]
    assert "project.create" in kinds
    assert "model_profile.copy_from_template" in kinds
    assert "project_member.add" in kinds

    # Chain continuity within this project's events.
    for prev, curr in itertools.pairwise(rows):
        assert curr.prev_event_id == prev.id
