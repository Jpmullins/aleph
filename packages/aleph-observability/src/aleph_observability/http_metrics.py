"""ASGI middleware that counts and times every HTTP request.

Pure ASGI, not `BaseHTTPMiddleware`, for two reasons. It has to see the status
line of a streaming response without buffering it — four of Aleph's surfaces are
long-lived SSE streams, and a middleware that materialises the body would break
them. And it has to record on the exception path, where there is no response
object at all.

## Why it is not in `app.user_middleware`

`apps/api/tests/unit/test_request_correlation.py::test_middleware_order_is_pinned`
asserts the user middleware stack is exactly four entries, and that tuple guards
two shipped defects (CORS outermost, RequestID outside Error). Adding a fifth
entry there is a decision for whoever owns that invariant, not a side effect of
adding metrics.

So this wraps the *built* stack instead — the same technique the upstream OTEL
FastAPI instrumentation uses, and for the same reason. `install_http_metrics`
is called from `instrument_fastapi`, after the OTEL wrap, so the timing covers
the entire stack including auth and tracing overhead: the number a user
experiences, not the number the handler experiences.

Because this is invisible to `app.user_middleware`, it is pinned by a test that
drives a request and asserts the counter moved
(`packages/aleph-observability/tests/test_http_metrics.py`). Removing the
install line has to turn something red.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

import structlog

from aleph_observability.metrics import record_http_request, route_template

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_log = structlog.get_logger(__name__)


class HttpMetricsMiddleware:
    """Counts requests and records latency, labelled by route TEMPLATE."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # 500 is the right default, not 0: if the app raises before sending a
        # response start, the client got a 500 from the server error handler
        # above us, and a metric that recorded "0" would put a fake status in
        # the label set nobody could interpret.
        status = 500
        started = time.perf_counter()

        async def _send(message: Message) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                raw = message.get("status")
                if isinstance(raw, int):
                    status = raw
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            duration_s = time.perf_counter() - started
            try:
                record_http_request(
                    route=route_template(scope),
                    method=str(scope.get("method", "")),
                    status=status,
                    duration_s=duration_s,
                )
            except Exception:
                # This runs in a `finally` that may be unwinding a real
                # exception. Letting a metrics bug replace the caller's
                # traceback would make the observability layer the thing that
                # hides the outage. Loud in the log, invisible to the request.
                _log.exception("metrics.http_record_failed")


_installed: WeakSet[Any] = WeakSet()


def install_http_metrics(app: Any) -> None:
    """Wrap the app's built middleware stack. Idempotent per app object."""
    if app in _installed:
        return
    original: Callable[[], ASGIApp] = app.build_middleware_stack

    def _build() -> ASGIApp:
        return HttpMetricsMiddleware(original())

    app.build_middleware_stack = _build
    _installed.add(app)


def is_http_metrics_installed(app: Any) -> bool:
    """Whether `install_http_metrics` has wrapped this app.

    Exists because the wrap is invisible to `app.user_middleware`, so a test
    that reads the middleware list cannot tell. See the module docstring.
    """
    return app in _installed
