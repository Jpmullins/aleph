"""OTEL + Langfuse + structlog wiring."""

from aleph_observability.langfuse_client import (
    LangfuseClient,
    init_langfuse,
    shutdown_langfuse,
)
from aleph_observability.langfuse_reader import (
    DiagnosticSnapshot,
    ErrorObservation,
    LangfuseReader,
)
from aleph_observability.logging import bind_request_context, configure_logging
from aleph_observability.tracing import (
    current_trace_id,
    init_otel,
    instrument_fastapi,
    instrument_httpx,
    instrument_sqlalchemy,
    shutdown_otel,
    start_span,
)

__all__ = [
    "DiagnosticSnapshot",
    "ErrorObservation",
    "LangfuseClient",
    "LangfuseReader",
    "bind_request_context",
    "configure_logging",
    "current_trace_id",
    "init_langfuse",
    "init_otel",
    "instrument_fastapi",
    "instrument_httpx",
    "instrument_sqlalchemy",
    "shutdown_langfuse",
    "shutdown_otel",
    "start_span",
]
