"""Cross-project permission leakage test.

Member A in Project X must not see Project Y. Response is 404 (NotFound),
never 403 — existence does not leak across project scopes.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_a(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-a", "email": "a@test.local", "name": "A"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


@pytest.fixture
async def auth_b(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-b", "email": "b@test.local", "name": "B"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_b_cannot_read_a_project(http_client, auth_a):
    # User A creates a project.
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "secret", "description": "", "budget_usd": "1.00"},
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]

    # Switch identity to user B and try to fetch.
    from aleph_api.middleware import auth as auth_mod

    async def fake_b(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-b", "email": "b@test.local", "name": "B"}

    auth_mod.verify_user_jwt = fake_b  # type: ignore[assignment]

    blocked = await http_client.get(f"/v1/projects/{pid}")
    assert blocked.status_code == 404, blocked.text

    ledger = await http_client.get(f"/v1/projects/{pid}/ledger")
    assert ledger.status_code == 404, ledger.text

    cost = await http_client.get(f"/v1/projects/{pid}/cost")
    assert cost.status_code == 404, cost.text
