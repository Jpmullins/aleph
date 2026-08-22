"""POST /v1/scholar/search reports upstream failures by cause, not as one 503.

The defect: `except ScholarUpstreamError: raise GatewayUnavailable(...)` mapped
every upstream failure to 503 "the upstream service is unavailable" — a 400
caused by a filter Aleph itself built wrong included. An operator reading the
API log saw "the gateway is down" when the real message was "your query syntax
is wrong", which is the kind of misdirection that costs days.

Same pattern as `test_scholar_routes.py`: the app is built without running the
lifespan, the DB-backed seams are swapped, and `app.state.scholar` is a real
`ScholarService` over `httpx.MockTransport`. So the route's own mapping runs for
real against controlled upstream statuses — no network, no database.

Note the deliberately small `deadline_s`: these tests exercise the exhausted
path, and the budget is what decides how long that takes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, Path

from aleph_api.deps import principal_dep
from aleph_api.middleware.project_scope import project_scope_dep
from aleph_api.routes.scholar import _UPSTREAM_DEFAULT_STATUS, _UPSTREAM_STATUS_MAP
from aleph_scholar import ScholarHttp, ScholarService
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    deadline_s: float = 0.3,
) -> FastAPI:
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    app.state.settings = SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret="unit-test-secret-0123456789abcdef0123456789abcdef",
        aleph_consensus_monthly_search_cap=200,
    )
    app.state.scholar = ScholarService(
        mailto="unit@aleph.local",
        http=ScholarHttp(
            mailto="unit@aleph.local",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            retry_wait_min=0.05,
            retry_wait_max=0.05,
            deadline_s=deadline_s,
        ),
    )

    principal = Principal(
        user_id=uuid4(), subject="unit", email="unit@test.local", actor_kind="user"
    )

    async def _fake_local_dev(_request: Any) -> Principal:
        return principal

    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)

    async def _fake_scope(
        project_id: Annotated[UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> UUID:
        p.cache_role(project_id, ProjectRole.OWNER.value)
        return project_id

    app.dependency_overrides[project_scope_dep] = _fake_scope
    return app


async def _search(app: FastAPI, **body: Any) -> httpx.Response:
    payload = {"provider": "openalex", "query": "graph neural networks", **body}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(f"/v1/projects/{uuid4()}/scholar/search", json=payload)


def _static(status: int, **kwargs: Any):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, **kwargs)

    return handler


async def test_upstream_400_is_a_4xx_carrying_the_upstream_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED BEFORE WS-E2: this produced a bare 503 with no reason."""
    app = _build_app(
        monkeypatch,
        _static(
            400,
            json={"error": "Invalid query parameters", "message": "filter is not a valid field"},
        ),
    )
    resp = await _search(app)

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "filter is not a valid field" in body["detail"]
    assert body["details"]["upstream_status"] == 400
    assert body["details"]["provider"] == "openalex"
    assert "Retry-After" not in resp.headers  # nothing to wait for


async def test_upstream_422_is_reported_as_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(monkeypatch, _static(422, json={"message": "rows must be an integer"}))
    resp = await _search(app)
    assert resp.status_code == 422, resp.text
    assert "rows must be an integer" in resp.json()["detail"]


async def test_upstream_404_is_reported_as_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(monkeypatch, _static(404, text="Not found"))
    resp = await _search(app)
    assert resp.status_code == 404, resp.text


async def test_an_upstream_auth_failure_is_not_blamed_on_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 between Aleph and OpenAlex is not something our caller can fix.

    Echoing it as 403 would tell an authenticated project member they lack
    permission on their own project, which is a different and much more
    alarming claim than the true one.
    """
    app = _build_app(monkeypatch, _static(403, json={"error": "polite pool blocked"}))
    resp = await _search(app)
    assert resp.status_code == _UPSTREAM_DEFAULT_STATUS == 502, resp.text
    assert "polite pool blocked" in resp.json()["detail"]


async def test_persistent_429_is_503_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAILED BEFORE WS-E2: 503 with no Retry-After header at all."""
    app = _build_app(monkeypatch, _static(429, headers={"Retry-After": "17"}))
    resp = await _search(app)

    assert resp.status_code == 503, resp.text
    assert resp.headers["Retry-After"] == "17"  # the upstream's own number
    assert resp.json()["details"]["upstream_status"] == 429


async def test_a_throttled_search_tells_the_operator_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 429 that produced `WS-E2` stops being silent about its cause.

    MEASURED 2026-08-22: eight concurrent searches, 0 of 8 succeeded, every one
    a real OpenAlex 429 with `Retry-After: 28409` (7.9 hours, on the deployment
    rather than the request). The same query from the host returned 200. The
    difference was `ALEPH_SCHOLAR_MAILTO=dev@aleph.local`, an undeliverable
    address, so the polite pool never applied — and nothing in the log said so.

    Asserted at the ROUTE, not at `ScholarHttp`, because the log record is where
    the sentence actually reaches a person: `_upstream_response` writes
    `error=str(exc)[:1000]` and deliberately keeps the deployment's mailto out
    of the response body, so this is the only channel that carries it.
    """
    import structlog

    app = _build_app(monkeypatch, _static(429))
    with structlog.testing.capture_logs() as logs:
        resp = await _search(app)

    assert resp.status_code == 503, resp.text
    unavailable = [e for e in logs if e.get("event") == "scholar upstream unavailable"]
    assert unavailable, f"no operator-facing record of the failure in {logs}"
    error = str(unavailable[-1].get("error", ""))
    assert "ALEPH_SCHOLAR_MAILTO" in error, error
    assert "unit@aleph.local" in error, error


async def test_persistent_5xx_is_503_with_a_default_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 with no Retry-After invites an immediate retry into the same wall."""
    app = _build_app(monkeypatch, _static(503))
    resp = await _search(app)

    assert resp.status_code == 503, resp.text
    assert int(resp.headers["Retry-After"]) > 0


async def test_a_transport_failure_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    app = _build_app(monkeypatch, handler)
    resp = await _search(app)
    assert resp.status_code == 503, resp.text
    assert resp.json()["details"]["upstream_status"] is None


async def test_a_successful_search_still_returns_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mapping change must not have turned the happy path into a Response."""
    app = _build_app(
        monkeypatch,
        _static(
            200,
            json={
                "results": [
                    {"id": "https://openalex.org/W1", "display_name": "A paper", "doi": None}
                ]
            },
        ),
    )
    resp = await _search(app)
    assert resp.status_code == 200, resp.text
    assert [w["title"] for w in resp.json()["works"]] == ["A paper"]


async def test_the_caller_can_choose_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """`deadline_s` reaches the transport rather than being validated and dropped.

    A body field that is accepted and then ignored is the dominant defect class
    in this codebase — a contract with no caller. Proven by effect: a 1s budget
    against an unfailing 503 must produce more upstream attempts than the
    instance's own 0.3s default would.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    app = _build_app(monkeypatch, handler, deadline_s=0.05)
    assert (await _search(app)).status_code == 503
    default_attempts = len(calls)

    calls.clear()
    assert (await _search(app, deadline_s=1.0)).status_code == 503
    assert len(calls) > default_attempts


def test_the_mapping_table_is_pinned() -> None:
    """The table is the contract; a silent edit here is a silent behaviour change."""
    assert _UPSTREAM_STATUS_MAP == {400: 400, 404: 404, 422: 422}
    assert _UPSTREAM_DEFAULT_STATUS == 502
