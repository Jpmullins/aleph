"""A 500 has to be findable in the log, and the log line has to name the request.

The failure this defends against is not a crash — it is a crash nobody can
locate. An unhandled exception used to write four keys (environment, event,
level, timestamp) and answer with no `x-request-id` header, so "I got a 500 at
14:22" was the entire bug report and there was no way to join it to a line in
the log. Every expensive item that starts from a browser-side failure was
expensive for exactly that reason.

Two mechanisms are pinned here, and they are independent on purpose:

  * `ErrorMiddleware` reads the id, the principal and the project out of the
    shared ASGI scope. This is what makes `user_id` possible at all —
    `AuthMiddleware` is a `BaseHTTPMiddleware`, so the contextvars it binds live
    in a task spawned below the error handler and never reach it. Reordering
    alone cannot fix that; only reading the request can.
  * `RequestIDMiddleware` sits outside `ErrorMiddleware` so the problem response
    passes back out through the header stamp.

The order test is here because the correlation no longer depends on the order —
which means an accidental reorder would break nothing visible until the next
time somebody needed a log line. It is the same class of silent positional
regression as the CORS one (see `test_cors_survives_errors.py`), so it gets the
same kind of guard.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog
from starlette.requests import Request

USER_ID = UUID("11111111-2222-4333-8444-555555555555")
PROJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BOOM_PATH = f"/v1/projects/{PROJECT_ID}/boom"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    """The real middleware stack, a fake principal, and one route that raises.

    The principal is faked rather than provisioned so this stays a unit test:
    `_principal_local_dev` reaches Postgres, and the thing under test is what
    the middleware does with a principal, not how it got one.
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
    application.state.settings = SimpleNamespace(aleph_auth_mode="local")

    # Declared here, not in `routes/`, because a route whose only job is to
    # explode has no business being reachable in production.
    @application.post("/v1/projects/{project_id}/boom")
    async def _boom(project_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        msg = f"deliberate failure in {project_id}"
        raise RuntimeError(msg)

    @application.post("/v1/projects/{project_id}/expected-boom")
    async def _expected(project_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        from aleph_core.errors import NotFound

        msg = f"no such thing in {project_id}"
        raise NotFound(msg)

    return application


async def _post(app: Any, path: str, **headers: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(path, headers=headers)


async def test_unhandled_500_returns_the_clients_own_request_id(app: Any) -> None:
    """The caller supplied a correlation id; the failure has to come back with it."""
    resp = await _post(app, BOOM_PATH, **{"x-request-id": "RID-12345"})

    assert resp.status_code == 500
    assert resp.headers.get("x-request-id") == "RID-12345", (
        "the 500 came back without the id the client sent, so the client cannot "
        "tell anyone which request failed"
    )


async def test_unhandled_500_mints_a_request_id_when_the_client_sent_none(app: Any) -> None:
    """A browser sends no id. The response still has to carry one to quote."""
    resp = await _post(app, BOOM_PATH)

    assert resp.status_code == 500
    assert resp.headers.get("x-request-id"), "no correlation id was minted for the failure"


async def test_problem_body_carries_the_request_id(app: Any) -> None:
    """A user copies what the screen shows, not what the network tab holds."""
    resp = await _post(app, BOOM_PATH, **{"x-request-id": "RID-BODY"})

    body = resp.json()
    assert body["status"] == 500
    assert body["request_id"] == "RID-BODY", (
        f"RFC 7807 body has no request id: {sorted(body)}. The id then only "
        "exists in a header the person reporting the bug never sees."
    )


async def test_an_expected_error_is_correlated_too(app: Any) -> None:
    """A 404 nobody can locate is the same problem in a smaller size.

    `AlephError` responses are the ones a user actually reports ("it says not
    found and I don't know why"), and they went back with no id either.
    """
    resp = await _post(
        app, f"/v1/projects/{PROJECT_ID}/expected-boom", **{"x-request-id": "RID-404"}
    )

    assert resp.status_code == 404
    assert resp.headers.get("x-request-id") == "RID-404"
    assert resp.json()["request_id"] == "RID-404"


async def test_log_line_names_the_request_the_user_and_the_project(app: Any) -> None:
    """The whole point: one grep on the id lands on the traceback.

    `project_id` comes from `scope['path_params']` and `user_id` from
    `request.state.principal`. Neither can arrive via contextvars — see the
    module docstring — so this test fails the moment the explicit read in
    `ErrorMiddleware` is removed, whatever the middleware order says.
    """
    with structlog.testing.capture_logs() as entries:
        resp = await _post(app, BOOM_PATH, **{"x-request-id": "RID-LOG"})

    assert resp.status_code == 500
    records = [e for e in entries if e.get("event") == "unhandled exception"]
    assert records, f"no 'unhandled exception' record was emitted; got {entries}"
    record = records[0]

    assert record.get("request_id") == "RID-LOG", (
        f"log line does not name the request: {sorted(record)}"
    )
    assert record.get("user_id") == str(USER_ID), (
        f"log line does not name the principal: {sorted(record)}"
    )
    assert record.get("project_id") == PROJECT_ID, (
        f"log line does not name the project: {sorted(record)}"
    )


def test_correlation_survives_a_failure_before_the_router_matched() -> None:
    """A 500 out of a middleware has no `path_params` — the log still names the project.

    This is the shape a dead database takes: `AuthMiddleware` raises while
    resolving the principal, before any route has matched. It is precisely when
    somebody is reading the log, so falling back to the path is worth the regex.
    """
    from aleph_api.middleware.errors import correlation_fields

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": BOOM_PATH,
        "raw_path": BOOM_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"x-request-id", b"RID-EARLY")],
        "server": ("testserver", 80),
        "state": {},
    }
    fields = correlation_fields(Request(scope))

    assert fields["request_id"] == "RID-EARLY", (
        "with no RequestIDMiddleware binding yet, the client's own header is "
        "the only id available and it was dropped"
    )
    assert fields["project_id"] == PROJECT_ID
    assert fields["user_id"] is None


def test_correlation_prefers_what_the_router_actually_bound() -> None:
    """The path regex is the fallback; the router's own binding is authoritative.

    Every project-scoped route today lives under `/projects/<uuid>/`, so the two
    agree and the regex alone would look sufficient. It is not: it is a guess
    about URL shape, and the first router mounted somewhere else takes its
    project id with it. This pins the branch that keeps working when that
    happens — the regex cannot match this path.
    """
    from aleph_api.middleware.errors import correlation_fields

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/v1/workspaces/somewhere-else",
        "raw_path": b"/v1/workspaces/somewhere-else",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [],
        "server": ("testserver", 80),
        "state": {},
        "path_params": {"project_id": PROJECT_ID},
    }
    assert correlation_fields(Request(scope))["project_id"] == PROJECT_ID


def test_correlation_does_not_invent_a_project_from_a_collection_route() -> None:
    """`/v1/projects` is not a project. A wrong id in a log is worse than none."""
    from aleph_api.middleware.errors import correlation_fields

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/v1/projects",
        "raw_path": b"/v1/projects",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [],
        "server": ("testserver", 80),
        "state": {},
    }
    assert correlation_fields(Request(scope))["project_id"] is None


def test_middleware_order_is_pinned() -> None:
    """`add_middleware` PREPENDS, so `user_middleware[0]` is the OUTERMOST layer.

    Two invariants live in this tuple and neither is visible at the call site:
    CORS outermost, or a 500 reaches the browser as a CORS failure naming the
    wrong subsystem; and RequestID outside Error, or the header stamp is skipped
    on the exception path. Both were shipped defects. Adding a fourth middleware
    in the wrong slot reintroduces one of them and nothing else notices.

    If you are here because you added a middleware: decide where it belongs,
    then update this tuple deliberately.
    """
    from aleph_api.main import create_app

    app = create_app()
    order = tuple(m.cls.__name__ for m in app.user_middleware)

    assert order == (
        "CORSMiddleware",
        "RequestIDMiddleware",
        "ErrorMiddleware",
        "AuthMiddleware",
    ), f"middleware stack is {order} — outermost first"
