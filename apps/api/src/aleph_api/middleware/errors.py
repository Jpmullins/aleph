"""Translate AlephError into RFC 7807 problem details, log everything else.

Every response built here carries the request id — in the `x-request-id` header
and in the problem body — and the log line an unhandled exception writes names
the request, the user, and, on a project-scoped route, the project.

Before that, an unhandled 500 logged exactly four keys — environment, event,
level, timestamp — and went back to the browser with no `x-request-id` at all,
even when the caller had sent one. A user saying "I got a 500 at 14:22" could
not be matched to any line in the log, and there was nothing to join a
browser-side failure onto a server-side record. Two causes, both fixed here:
`RequestIDMiddleware` stamps the header on the response it gets back from
`call_next`, and on the exception path there is no response to stamp — the
exception passes straight through that frame; and nothing on this path ever
looked at the request.

Correlation is read HERE, out of the request, rather than inherited from the
contextvars `RequestIDMiddleware` and `AuthMiddleware` bind. That is not
belt-and-braces, it is the only thing that can work for `user_id`: both are
`BaseHTTPMiddleware`, whose `call_next` runs the downstream app in a task
spawned inside `dispatch`, and a task starts from a *copy* of the context — so a
binding made downstream never reaches an upstream middleware's frame.
`AuthMiddleware` is downstream of this middleware in every ordering (it has to
be: this middleware exists to catch what Auth raises), so waiting for its
`user_id` binding to show up here would wait forever. `request.state` and
`request.scope["path_params"]` DO cross that boundary, because every layer wraps
the same `scope` dict — which is why they are what this module reads.

Reading the request also makes the correlation independent of middleware order,
so the fix cannot be undone by an insertion in `main.py`. The order still
matters for the CORS invariant and is pinned by
`tests/unit/test_request_correlation.py::test_middleware_order_is_pinned`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from aleph_core.errors import AlephError

if TYPE_CHECKING:
    from collections.abc import Mapping

_log = structlog.get_logger(__name__)

# Fallback for an exception raised BEFORE the router matched, when the scope has
# no `path_params` yet — Postgres unreachable while `AuthMiddleware` provisions
# the principal is the realistic case, and it is exactly the moment somebody is
# reading these logs. Anchored on a UUID so `/v1/projects` collection routes and
# a literal segment can never be mistaken for an id.
_PROJECT_IN_PATH = re.compile(
    r"/projects/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


class ErrorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        try:
            return await call_next(request)
        except AlephError as exc:
            return _problem(exc, request)
        except Exception:
            fields = correlation_fields(request)
            # Passed as event kwargs, not bound to contextvars: see the module
            # docstring. Removing this argument is the mutation the log test
            # exists to catch.
            _log.exception("unhandled exception", **fields)
            body: dict[str, Any] = {
                "type": "about:blank#internal_error",
                "title": "Internal error",
                "status": 500,
                "detail": "An unexpected error occurred.",
                "instance": str(request.url.path),
            }
            return _respond(body, 500, fields["request_id"])


def correlation_fields(request: Request) -> dict[str, str | None]:
    """The three fields that make a failure findable. Must never raise.

    This runs while handling an exception. Anything it throws replaces a real
    500 with a second, unrelated one and loses the traceback the first one
    carried — so every lookup here is defensive on purpose.
    """
    rid = getattr(request.state, "request_id", "") or request.headers.get("x-request-id", "")
    principal = getattr(request.state, "principal", None)
    user_id = getattr(principal, "user_id", None)
    return {
        "request_id": str(rid) if rid else None,
        "user_id": str(user_id) if user_id is not None else None,
        "project_id": _project_id(request),
    }


def _project_id(request: Request) -> str | None:
    params = cast("Mapping[str, object]", request.scope.get("path_params") or {})
    raw = params.get("project_id")
    if raw is not None:
        return str(raw)
    match = _PROJECT_IN_PATH.search(request.url.path)
    return match.group(1) if match else None


def _respond(body: dict[str, Any], status: int, request_id: str | None) -> Response:
    if request_id:
        # In the body because a user reporting a failure copies what the screen
        # shows, not what the network tab holds; the header alone is not
        # reachable by the person who hit the error.
        body["request_id"] = request_id
    resp = JSONResponse(body, status_code=status, media_type="application/problem+json")
    if request_id:
        resp.headers["x-request-id"] = request_id
    return resp


def _problem(exc: AlephError, request: Request) -> Response:
    status = exc.http_status
    body: dict[str, Any] = {
        "type": f"about:blank#{exc.code}",
        "title": exc.code.replace("_", " ").capitalize(),
        "status": status,
        "detail": exc.message,
        "instance": str(request.url.path),
    }
    if exc.detail:
        body["details"] = exc.detail
    return _respond(body, status, correlation_fields(request)["request_id"])
