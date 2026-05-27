"""Tenacity retry policy for LiteLLM gateway calls.

Spec §0.8: 3 attempts, exponential backoff (1s, 2s, 4s), retry on
5xx + 429 + connection errors. Never retry on 4xx other than 429.
"""

from __future__ import annotations

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def gateway_retry() -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
