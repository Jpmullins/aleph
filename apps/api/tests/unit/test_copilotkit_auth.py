"""The AG-UI agent endpoint sits inside the principal boundary.

`AuthMiddleware` used to skip its bearer check for the entire `/copilotkit`
prefix, on a comment that said "the route handler is responsible for its own
verification". No such verification existed: `setup_copilotkit` mounts the
LangGraph AG-UI endpoint with no `dependencies=` and no principal. Combined with
agent tools that derive their project scope from a client-supplied `thread_id`
(`proj:<project_id>:<thread>`) with no membership check, anyone able to reach
the API — or the runtime bridge in front of it — could drive the Deep Agent's
*write* tools against an arbitrary project UUID.

These tests pin the boundary. They deliberately start no lifespan and no DB:
the middleware only needs `app.state.settings`, so a regression that let the
request through would hit unset state and 500 rather than 401 — either way the
assertion fails.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

AGENT_PATHS = [
    "/copilotkit/agent/assistant",
    "/copilotkit/agent/assistant/run",
    "/copilotkit",
]


@pytest.mark.parametrize("path", AGENT_PATHS)
async def test_copilotkit_is_not_exempt_from_auth(path: str) -> None:
    """No credential ⇒ 401, never a 200 and never an unauthenticated run."""
    from aleph_api.main import create_app

    app = create_app()
    app.state.settings = SimpleNamespace(aleph_auth_mode="local")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(path, json={"messages": []})

    # No database is configured here, so resolving the local principal fails and
    # the request errors. That IS the assertion: the request reached
    # AuthMiddleware instead of sailing past it. A 2xx would mean the endpoint
    # is exempt again, which is the vulnerability this pins.
    assert resp.status_code >= 400, (
        f"{path} accepted an unauthenticated request (got {resp.status_code}). "
        "The AG-UI agent endpoint must sit inside the principal boundary — its "
        "tools write to project state."
    )


def test_no_blanket_auth_exemption_prefixes() -> None:
    """Guard the mechanism, not just one path.

    A prefix exemption is a standing invitation to mount a write route behind
    it by accident. If a future exemption is genuinely needed, this test must be
    updated deliberately and the exempted handler must do its own verification —
    which is exactly the claim that turned out to be false for `/copilotkit`.
    """
    from aleph_api.middleware.auth import _SELF_AUTH_PREFIXES

    assert _SELF_AUTH_PREFIXES == (), (
        f"auth middleware blanket-exempts {_SELF_AUTH_PREFIXES!r}. Every route "
        "under an exempted prefix is unauthenticated in BOTH auth modes."
    )


def test_public_paths_are_read_only() -> None:
    """The remaining unauthenticated surface must not mutate anything."""
    from aleph_api.middleware.auth import _PUBLIC_PATHS

    allowed = {"/healthz", "/readyz", "/docs", "/redoc", "/openapi.json"}
    unexpected = _PUBLIC_PATHS - allowed
    assert not unexpected, f"unexpected unauthenticated path(s): {sorted(unexpected)}"
