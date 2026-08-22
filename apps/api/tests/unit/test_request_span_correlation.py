"""One id joins the log line, the span and the response.

WS-P9 criterion 5. WS-P2 landed two of the three legs — the response header and
the log record — and `test_request_correlation.py` pins those. The trace was
still unjoined: a request id in a log line and a trace in Langfuse had nothing
in common, so "find the trace for the 500 this user reported" meant guessing by
timestamp.

The fix is deliberately not a fourth place that computes an id. `bind_request_context`
now writes the span attribute from the same argument it binds to the log
context, so the three legs cannot drift: they are one variable.

The test drives a real 500 through the real middleware stack because that is the
case a person actually reports, and because it is the path where an id is
hardest to keep — an exception is not a response, so every frame that stamps
something on the way out gets skipped.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.requests import Request

from aleph_observability.logging import SPAN_REQUEST_ID, SPAN_USER_ID

USER_ID = UUID("11111111-2222-4333-8444-555555555555")
PROJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BOOM_PATH = f"/v1/projects/{PROJECT_ID}/span-boom"
RID = "RID-SPAN-CORRELATION"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """Capture spans off whatever SDK provider this process ended up with.

    `set_tracer_provider` refuses to be overridden, and pytest shares one
    process across the suite, so "install my own provider" is not something a
    test can assume. Attaching a processor to the installed one works either
    way; the only case that cannot work is no SDK provider at all, and that is
    asserted rather than skipped — a silent skip here would mean the criterion
    is unverified while the suite reports green.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        trace.set_tracer_provider(TracerProvider())
        provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        "no OTEL SDK tracer provider is installed, so no span can carry the "
        f"request id; got {type(provider).__name__}"
    )
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    return exp


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter) -> Any:
    del exporter  # ordering only: the processor must exist before instrumentation
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

    @application.post("/v1/projects/{project_id}/span-boom")
    async def _boom(project_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        msg = f"deliberate failure in {project_id}"
        raise RuntimeError(msg)

    return application


def _spans_with_request_id(exporter: InMemorySpanExporter, rid: str) -> list[ReadableSpan]:
    return [
        span
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get(SPAN_REQUEST_ID) == rid
    ]


async def test_the_response_the_log_and_the_span_carry_the_same_id(
    app: Any, exporter: InMemorySpanExporter
) -> None:
    with structlog.testing.capture_logs() as entries:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(BOOM_PATH, headers={"x-request-id": RID})

    assert resp.status_code == 500

    # 1. the response the user is holding
    assert resp.headers.get("x-request-id") == RID
    assert resp.json()["request_id"] == RID

    # 2. the log line somebody will grep
    records = [e for e in entries if e.get("event") == "unhandled exception"]
    assert records, f"no 'unhandled exception' record: {entries}"
    assert records[0].get("request_id") == RID

    # 3. the span the trace backend indexes
    matched = _spans_with_request_id(exporter, RID)
    assert matched, (
        f"no span carries {SPAN_REQUEST_ID}={RID}. The log line and the "
        "response agree and the trace is still unreachable from either, which "
        "is the state WS-P9 criterion 5 exists to end. Attributes seen: "
        f"{[dict(s.attributes or {}) for s in exporter.get_finished_spans()]}"
    )


async def test_the_span_also_names_the_principal(app: Any, exporter: InMemorySpanExporter) -> None:
    """`user_id` reaches the span for the same reason it reaches the log line.

    It is bound by `AuthMiddleware`, which runs inside the server span, so the
    same one call that makes the log record findable makes the trace findable.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post(BOOM_PATH, headers={"x-request-id": RID})

    matched = _spans_with_request_id(exporter, RID)
    assert matched, "no span carried the request id at all"
    assert any((span.attributes or {}).get(SPAN_USER_ID) == str(USER_ID) for span in matched), (
        "the span names the request but not who made it"
    )
