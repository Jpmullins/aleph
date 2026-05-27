"""Assigns a request id to every incoming request and binds it to structlog."""

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
