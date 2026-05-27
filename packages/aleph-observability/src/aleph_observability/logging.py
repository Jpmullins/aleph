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


def bind_request_context(
    *,
    request_id: str,
    user_id: UUID | None = None,
    project_id: UUID | None = None,
) -> None:
    """Bind request-scoped fields to the structlog contextvars."""
    bindings: dict[str, Any] = {"request_id": request_id}
    if user_id is not None:
        bindings["user_id"] = str(user_id)
    if project_id is not None:
        bindings["project_id"] = str(project_id)
    structlog.contextvars.bind_contextvars(**bindings)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
