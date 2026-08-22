"""`/metrics` exposes real series, and it is not readable by the network.

## What "not public" can honestly be asserted here

`docs/plan.md` WS-P9 criterion 6 asks for "GET /metrics without a credential in
oidc mode returns 401". There is no oidc mode — it was removed
(`docs/decisions.md` D6) — and in `local` mode `AuthMiddleware` *synthesises* a
dev principal for any request that is not carrying an agent token. So a request
with no credential is not an unauthenticated request in this system, and a test
asserting 401 for one could only be made to pass by faking it. That is the
failure mode Part 0 of the plan is about, so it is not written.

What IS asserted, and what can each fail:

  * `/metrics` is not in `_PUBLIC_PATHS`, so it runs the same auth middleware as
    every other route — proven by driving a malformed agent token at it and
    getting the 401 the middleware produces. Adding `/metrics` to that set makes
    this return 200 and the test red.
  * The handler refuses a peer that is neither loopback nor bearing
    `ALEPH_METRICS_TOKEN`. Port 8000 is published on 0.0.0.0, so this is the
    check that actually stands between a LAN and the endpoint.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from starlette.requests import Request

USER_ID = UUID("11111111-2222-4333-8444-555555555555")
TOKEN = "s3cr3t-scrape-token"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real app, with the principal faked so this stays a unit test.

    `_principal_local_dev` reaches Postgres; what is under test is the route's
    own decision, not how a principal was provisioned.
    """
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mod
    from aleph_security.principal import Principal

    async def _fake_principal(_request: Request) -> Principal:
        return Principal(
            user_id=USER_ID,
            subject="dev|test",
            email="dev@aleph.local",
            actor_kind="user",
        )

    monkeypatch.setattr(auth_mod, "_principal_local_dev", _fake_principal)

    application = create_app()
    # No redis and no session_maker on purpose: the pull gauges must degrade to
    # "not reported" rather than 500 the scrape. A metrics endpoint that dies
    # with its dependencies is missing at exactly the moment it is wanted.
    application.state.settings = SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret="unit-test-secret",
    )
    return application


async def _get(app: Any, *, client: tuple[str, int] = ("127.0.0.1", 1234), **headers: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client),
        base_url="http://testserver",
    ) as http:
        return await http.get("/metrics", headers=headers)


def _fake_agent_token() -> str:
    """An HS256-shaped JWT the middleware will try, and fail, to verify."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=")
    return f"{header.decode()}.eyJzdWIiOiJ4In0.not-a-valid-signature"


# ---------------------------------------------------------------------------
# It is on the authenticated path
# ---------------------------------------------------------------------------


def test_metrics_is_not_in_public_paths() -> None:
    """The one-line change that would turn this into an anonymous endpoint."""
    from aleph_api.middleware.auth import _PUBLIC_PATHS

    assert "/metrics" not in _PUBLIC_PATHS, (
        "/metrics was added to the auth bypass list alongside /healthz and "
        "/docs. Port 8000 is published on 0.0.0.0; this makes the endpoint "
        "readable by anything that can reach the host."
    )


async def test_a_bad_credential_is_refused_by_the_middleware(app: Any) -> None:
    """Proves (1) above by observing the middleware's own answer, not the list.

    A grep for `_PUBLIC_PATHS` is a text check. This drives a request that the
    middleware must reject and asserts it did — so an exemption added by any
    other mechanism (a new prefix, a `dependencies=[]` hole) also shows up.
    """
    resp = await _get(app, authorization=f"Bearer {_fake_agent_token()}")
    assert resp.status_code == 401, (
        f"a forged agent token got {resp.status_code} from /metrics; the route "
        "is not running the auth middleware"
    )


# ---------------------------------------------------------------------------
# The handler's own gate
# ---------------------------------------------------------------------------


async def test_a_non_local_peer_is_refused_when_no_token_is_configured(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aleph_api.routes.metrics import METRICS_TOKEN_ENV

    monkeypatch.delenv(METRICS_TOKEN_ENV, raising=False)
    resp = await _get(app, client=("10.1.2.3", 5000))
    assert resp.status_code == 403, (
        f"a request from 10.1.2.3 got {resp.status_code}; with no token "
        "configured the endpoint is readable from the network"
    )


async def test_a_local_peer_is_served_when_no_token_is_configured(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zero-config path: a scraper sharing the container's loopback."""
    from aleph_api.routes.metrics import METRICS_TOKEN_ENV

    monkeypatch.delenv(METRICS_TOKEN_ENV, raising=False)
    resp = await _get(app)
    assert resp.status_code == 200, resp.text


async def test_the_token_is_required_once_it_is_configured(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And loopback is NOT a bypass — configuring a token means enforcing it."""
    from aleph_api.routes.metrics import METRICS_TOKEN_ENV

    monkeypatch.setenv(METRICS_TOKEN_ENV, TOKEN)

    assert (await _get(app)).status_code == 403, "no bearer, still served"
    assert (await _get(app, authorization="Bearer wrong-token")).status_code == 403, (
        "any bearer was accepted"
    )

    ok = await _get(app, client=("10.1.2.3", 5000), authorization=f"Bearer {TOKEN}")
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# It exposes something worth scraping
# ---------------------------------------------------------------------------


async def test_the_exposition_carries_at_least_twelve_aleph_series(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WS-P9 criterion 1, run in-process instead of with curl.

    Driving the route also generates one of the series it reports, which is the
    read-path proof the workstream's second risk asks for: the endpoint is
    queried by something, not merely written to.
    """
    from aleph_api.routes.metrics import METRICS_TOKEN_ENV

    monkeypatch.delenv(METRICS_TOKEN_ENV, raising=False)
    await _get(app)  # warm: the first scrape has no HTTP series of its own yet
    resp = await _get(app)

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("aleph_")]
    assert len(lines) >= 12, f"only {len(lines)} aleph_ sample lines:\n{resp.text}"


async def test_the_metrics_route_labels_itself_by_template_not_by_path(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aleph_api.routes.metrics import METRICS_TOKEN_ENV

    monkeypatch.delenv(METRICS_TOKEN_ENV, raising=False)
    await _get(app)
    resp = await _get(app)
    assert 'aleph_http_requests_total{method="GET",route="/metrics",status="200"}' in resp.text
