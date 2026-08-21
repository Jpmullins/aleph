"""Assigns a request id to every incoming request and binds it to structlog.

Position matters: this must be OUTSIDE `ErrorMiddleware`. The stamp below runs
on a response, and an exception is not a response — it passes through this frame
without one, so when this middleware sat inside the error handler every 500 went
back with no `x-request-id`, including when the caller had supplied the id
itself. `main.py` documents the whole ordering and a test pins it.

The id is written to `request.state` as well as to the structlog contextvars,
because `request.state` is backed by the ASGI scope and so is readable from
every layer, while a contextvar bound here only reaches layers BELOW this one —
`call_next` runs them in a spawned task, which gets a copy of the context.
"""

from __future__ import annotations

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from aleph_observability.logging import (
    bind_request_context,
    clear_request_context,
)


class RequestIDMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        rid = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = rid
        bind_request_context(request_id=rid)
        try:
            response: Response = await call_next(request)
        finally:
            clear_request_context()
        response.headers["x-request-id"] = rid
        return response
