"""Cross-project permission leakage test.

Member A in Project X must not see Project Y. Response is 404 (NotFound),
never 403 — existence does not leak across project scopes.

This test only has teeth in **oidc** auth mode: it relies on two *distinct*
principals (user-a, user-b). In `local` mode the middleware collapses every
request to the single dev principal (see `_principal_local_dev`), so A and B
would be the same user and the isolation check is vacuous. The `oidc_client`
fixture therefore builds a fresh app in oidc mode with `verify_user_jwt`
monkey-patched per identity (no JWKS network call).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def oidc_client() -> AsyncIterator[httpx.AsyncClient]:
    """Test client backed by an app running in `oidc` auth mode.

    The shared `http_client`/`asgi_app` fixtures build the app in `local`
    mode, where the middleware never calls `verify_user_jwt`. Here we flip
    `ALEPH_AUTH_MODE=oidc` (clearing the `get_settings` lru_cache so the new
    value is picked up) and build a dedicated app. `verify_user_jwt` is
    monkey-patched in the test body, so the constructed `JWKSCache` is never
    hit over the network. The conftest already defaults the issuer/audience/
    jwks-url env vars the oidc lifespan branch requires.
    """
    from aleph_api.main import create_app
    from aleph_api.settings import get_settings

    os.environ["ALEPH_AUTH_MODE"] = "oidc"
    get_settings.cache_clear()
    try:
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Authorization": "Bearer test"},
            ) as client,
        ):
            yield client
    finally:
        # Restore the local-mode default so other integration tests are
        # unaffected (env revert + cache clear, in that order).
        os.environ.pop("ALEPH_AUTH_MODE", None)
        get_settings.cache_clear()


def _claims_for(sub: str, email: str, name: str):
    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": sub, "email": email, "name": name}

    return fake_verify


async def test_b_cannot_read_a_project(oidc_client, monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    # User A creates a project.
    monkeypatch.setattr(auth_mod, "verify_user_jwt", _claims_for("user-a", "a@test.local", "A"))
    resp = await oidc_client.post(
        "/v1/projects",
        json={"title": "secret", "description": ""},
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    # Switch identity to user B and try to fetch A's project.
    monkeypatch.setattr(auth_mod, "verify_user_jwt", _claims_for("user-b", "b@test.local", "B"))

    blocked = await oidc_client.get(f"/v1/projects/{pid}")
    assert blocked.status_code == 404, blocked.text

    ledger = await oidc_client.get(f"/v1/projects/{pid}/ledger")
    assert ledger.status_code == 404, ledger.text

    cost = await oidc_client.get(f"/v1/projects/{pid}/cost")
    assert cost.status_code == 404, cost.text
