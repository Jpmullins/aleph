"""Langfuse client wrapper.

Langfuse 4.x consumes OTEL spans natively, so once `init_otel` is
configured to export OTLP to the collector (which forwards to
Langfuse), traces appear in the Langfuse UI automatically. This module
adds a thin convenience wrapper to:

  * tag the currently active span as a Langfuse "trace" or "span" with
    well-known names that the Langfuse UI groups on, and
  * write evaluation records / user feedback (Inc 8) without each
    caller importing the SDK directly.
"""

from __future__ import annotations

from langfuse import Langfuse

_client: Langfuse | None = None


class LangfuseClient:
    """Thin facade over the Langfuse SDK."""

    def __init__(self, client: Langfuse) -> None:
        self._client = client

    def flush(self) -> None:
        self._client.flush()


def init_langfuse(
    *,
    host: str,
    public_key: str,
    secret_key: str,
) -> LangfuseClient:
    global _client
    if _client is None:
        _client = Langfuse(
            host=host,
            public_key=public_key,
            secret_key=secret_key,
            timeout=10,
        )
    return LangfuseClient(_client)


def shutdown_langfuse() -> None:
    global _client
    if _client is not None:
        _client.flush()
        _client = None
