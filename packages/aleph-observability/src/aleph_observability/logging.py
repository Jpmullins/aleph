"""structlog configuration.

JSON output, ISO-8601 timestamps, OTEL trace/span context injection,
request-scoped binding helpers.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import structlog
from opentelemetry import trace


def _add_otel_context(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(*, environment: str, level: str = "INFO") -> None:
    """Install JSON structlog processors. Idempotent."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_otel_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Tag environment as a permanent context entry.
    structlog.contextvars.bind_contextvars(environment=environment)


#: Span attribute names, kept next to the log keys they mirror so the two
#: cannot drift into naming the same value differently.
SPAN_REQUEST_ID = "aleph.request_id"
SPAN_USER_ID = "aleph.user_id"
SPAN_PROJECT_ID = "aleph.project_id"


def bind_request_context(
    *,
    request_id: str,
    user_id: UUID | None = None,
    project_id: UUID | None = None,
) -> None:
    """Bind request-scoped fields to the structlog contextvars AND the span.

    One call writes both sinks on purpose. The whole point of a correlation id
    is that a user saying "I got a 500 at 14:22" hands over a string that lands
    on a log line, a trace and the response they were holding — and three sites
    that each compute "the id" separately is how those three stop agreeing.
    Here they are the same variable.

    The response is the third sink: `RequestIDMiddleware` stamps `x-request-id`
    from the same `rid` it passes in here, and CORS `expose_headers` makes it
    readable by the page. See `apps/api/src/aleph_api/main.py`.

    Callers run inside the OTEL server span (the FastAPI instrumentation wraps
    the whole middleware stack), so `get_current_span` is that span. When there
    is none — a worker, a test — `INVALID_SPAN` absorbs the writes and nothing
    raises, which is why there is no `is_recording()` dance here.
    """
    bindings: dict[str, Any] = {"request_id": request_id}
    if user_id is not None:
        bindings["user_id"] = str(user_id)
    if project_id is not None:
        bindings["project_id"] = str(project_id)
    structlog.contextvars.bind_contextvars(**bindings)

    span = trace.get_current_span()
    if request_id:
        span.set_attribute(SPAN_REQUEST_ID, request_id)
    if user_id is not None:
        span.set_attribute(SPAN_USER_ID, str(user_id))
    if project_id is not None:
        span.set_attribute(SPAN_PROJECT_ID, str(project_id))


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
