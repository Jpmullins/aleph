"""Polite shared HTTP transport for scholarly upstreams.

One `ScholarHttp` instance is shared by the Crossref and OpenAlex clients:

- `User-Agent: aleph-scholar/0.1 (mailto:<mailto>)` on every request
  (Crossref polite pool; OpenAlex additionally gets `mailto=` as a param).
- Per-host token bucket at 1 req/s with burst 5.
- Tenacity retry mirroring `aleph_models.retry.gateway_retry`: 3 attempts,
  exponential backoff 1-4s, retry on transport errors / timeouts / 429 / 5xx,
  reraise. A `Retry-After` header is honored (capped at 30s).

`get()` returns the response for any non-retryable status — including 404,
which callers treat as an authoritative-missing signal. Exhausted retries
and transport failures surface as `ScholarUpstreamError` so callers can map
them to the tri-state `ok=None` verdict.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from aleph_scholar.errors import ScholarUpstreamError

_RETRY_AFTER_CAP_S = 30.0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a numeric `Retry-After` header, capped at 30s."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None  # HTTP-date form — fall back to exponential backoff
    return max(0.0, min(seconds, _RETRY_AFTER_CAP_S))


class _TokenBucket:
    """1 req/s token bucket with a configurable burst, per host."""

    def __init__(
        self,
        *,
        rate: float,
        burst: int,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await self._sleep(wait)
                self._updated = self._clock()
                self._tokens = min(self._burst, 1.0)
            self._tokens -= 1.0


def ensure_ok(response: httpx.Response) -> httpx.Response:
    """Raise `ScholarUpstreamError` on any non-2xx the caller didn't handle.

    The retry layer already absorbed 429/5xx; whatever 4xx reaches a caller
    (bad filter syntax, auth changes, upstream quirks) is an upstream fault,
    not a crash — tri-state consumers fold it to `ok=None` (spec §1), and
    the reviewer's doi_verification node must never fail the review run.
    """
    if response.status_code >= 400:
        msg = f"scholar upstream returned HTTP {response.status_code} for {response.request.url}"
        raise ScholarUpstreamError(msg)
    return response


class ScholarHttp:
    """Shared httpx.AsyncClient wrapper with politeness + retry built in."""

    def __init__(
        self,
        *,
        mailto: str,
        client: httpx.AsyncClient | None = None,
        rate_per_second: float = 1.0,
        burst: int = 5,
        retry_attempts: int = 3,
        retry_wait_min: float = 1.0,
        retry_wait_max: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.mailto = mailto
        self.user_agent = f"aleph-scholar/0.1 (mailto:{mailto})"
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._rate = rate_per_second
        self._burst = burst
        self._retry_attempts = retry_attempts
        self._retry_wait_min = retry_wait_min
        self._retry_wait_max = retry_wait_max
        self._clock = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._buckets: dict[str, _TokenBucket] = {}

    def _bucket(self, host: str) -> _TokenBucket:
        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = _TokenBucket(
                rate=self._rate, burst=self._burst, clock=self._clock, sleep=self._sleep
            )
            self._buckets[host] = bucket
        return bucket

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=1, min=self._retry_wait_min, max=self._retry_wait_max),
            reraise=True,
        )

    async def get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        """GET with politeness + retry.

        Returns the response for any non-retryable status (404 included —
        the caller decides what "missing" means). Raises
        `ScholarUpstreamError` when transport keeps failing or 429/5xx
        persists through all retry attempts.
        """
        host = httpx.URL(url).host or ""
        try:
            async for attempt in self._retrying():
                with attempt:
                    await self._bucket(host).acquire()
                    response = await self._client.get(
                        url, params=params, headers={"User-Agent": self.user_agent}
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        retry_after = _retry_after_seconds(response)
                        if retry_after is not None:
                            await self._sleep(retry_after)
                        response.raise_for_status()
                    return response
        except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            msg = f"scholar upstream {host} unavailable: {exc!r}"
            raise ScholarUpstreamError(msg) from exc
        except RetryError as exc:  # pragma: no cover — reraise=True makes this unreachable
            msg = f"scholar upstream {host} unavailable: {exc!r}"
            raise ScholarUpstreamError(msg) from exc
        msg = f"scholar upstream {host}: retry loop exited without a response"
        raise ScholarUpstreamError(msg)  # pragma: no cover — defensive

    async def aclose(self) -> None:
        await self._client.aclose()
