"""OTEL tracer + span context utilities.

`init_otel` is called once at app startup. It configures the OTLP gRPC
exporter at `OTEL_EXPORTER_OTLP_ENDPOINT`, sets the service resource,
and installs instrumentations for FastAPI, httpx, SQLAlchemy.

The Langfuse SDK is configured separately (see `langfuse_client`) and
listens to the same OTEL provider so spans flow into both backends.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI

_TRACER_NAME = "aleph"
_provider: TracerProvider | None = None


def init_otel(
    *,
    service_name: str,
    service_version: str,
    environment: str,
    profile: str,
    otlp_endpoint: str,
) -> None:
    """Configure the global tracer provider. Idempotent."""
    global _provider
    if _provider is not None:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": environment,
            "aleph.profile": profile,
        }
    )
    provider = TracerProvider(resource=resource)
    # An empty endpoint means "do not export". Tracing is opt-in — the
    # collector and Langfuse sit behind the `tracing` compose profile — but the
    # exporter was configured unconditionally, so on the default stack every
    # process retried a connection to a host that does not exist, forever.
    #
    # That is not merely noisy: it buried real errors. Diagnosing an agent
    # failure meant reading past hundreds of "Failed to export traces to
    # otel-collector:4317" lines to find the one line that mattered, and a log
    # nobody can read is a log nobody reads.
    #
    # The provider is still installed, so spans are created and context
    # propagates in-process; only the network exporter is skipped.
    if otlp_endpoint.strip():
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _provider = provider


def shutdown_otel() -> None:
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def _tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def start_span(name: str, **attrs: Any) -> Iterator[Span]:
    """Open a span with the given name and attributes.

    On exception, the span is marked ERROR and the exception is recorded.
    """
    tracer = _tracer()
    with tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            span.set_attribute(k, v)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def current_trace_id() -> str | None:
    """Return the active trace id as hex, or None if no span is active."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or ctx.trace_id == 0:
        return None
    return format(ctx.trace_id, "032x")


def instrument_fastapi(app: FastAPI) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def instrument_httpx() -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def instrument_sqlalchemy(engine: Any) -> None:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    # For async engine, instrument the sync engine inside it.
    sync_engine = getattr(engine, "sync_engine", engine)
    SQLAlchemyInstrumentor().instrument(engine=sync_engine)
