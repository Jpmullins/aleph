"""Translate AlephError into RFC 7807 problem details, log everything else."""

from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from aleph_core.errors import AlephError

_log = structlog.get_logger(__name__)


class ErrorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        try:
            return await call_next(request)
        except AlephError as exc:
            return _problem(exc, request)
        except Exception:
            _log.exception("unhandled exception")
            body = {
                "type": "about:blank#internal_error",
                "title": "Internal error",
                "status": 500,
                "detail": "An unexpected error occurred.",
                "instance": str(request.url.path),
            }
            return JSONResponse(
                body, status_code=500, media_type="application/problem+json"
            )


def _problem(exc: AlephError, request: Request) -> Response:
    status = exc.http_status
    body = {
        "type": f"about:blank#{exc.code}",
        "title": exc.code.replace("_", " ").capitalize(),
        "status": status,
        "detail": exc.message,
        "instance": str(request.url.path),
    }
    if exc.detail:
        body["details"] = exc.detail
    return JSONResponse(
        body, status_code=status, media_type="application/problem+json"
    )
