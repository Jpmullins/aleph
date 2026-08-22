"""Polite shared HTTP transport for scholarly upstreams.

One `ScholarHttp` instance is shared by the Crossref and OpenAlex clients:

- `User-Agent: aleph-scholar/0.1 (mailto:<mailto>)` on every request
  (Crossref polite pool; OpenAlex additionally gets `mailto=` as a param).
- Per-host token bucket, default 5 req/s with burst 10 (see below).
- Single-flight de-duplication: identical concurrent GETs share one upstream
  request.
- A per-request **deadline** rather than a fixed attempt count: retries of
  429/5xx and transport errors continue until the caller's wall-clock budget
  runs out.

`get()` returns the response for any non-retryable status — including 404,
which callers treat as an authoritative-missing signal. `ensure_ok()` turns a
4xx into `ScholarClientError` (the request was rejected — actionable), while
a 429/5xx that outlives the deadline, and any transport failure, surface as
`ScholarUnavailable` (retry later). Both subclass `ScholarUpstreamError`, so
tri-state consumers still fold either to `ok=None`.

Three defects this module's shape exists to prevent:

1. **Every upstream fault wearing one error code.** `ensure_ok` used to raise
   the same exception for a 400 caused by Aleph's own malformed filter and for
   a dead network, and the API mapped both to 503 "service unavailable". The
   split into `ScholarClientError` / `ScholarUnavailable` is what lets the
   route tell a caller which of those actually happened.
2. **A retry budget expressed in attempts.** `stop_after_attempt(3)` with an
   8s `Retry-After` cap is neither "try hard" nor "fail fast" — it is an
   arbitrary number whose wall-clock meaning changes with upstream latency.
   The budget is now seconds, chosen by the caller, and it bounds the token
   bucket wait too: a throttled request fails on the deadline instead of
   hanging behind the queue.
3. **The rate limit serialising the research loop.** 1 req/s meant eight
   concurrent literature searches took eight seconds of pure waiting before
   any upstream latency. OpenAlex's polite pool (which is what `mailto=` buys)
   is 10 req/s; the default here is deliberately half of that, and
   `POLITE_POOL_CEILING_PER_SECOND` clamps whatever an operator configures so
   the deployment's mailto cannot be configured into a block.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from aleph_scholar.errors import ScholarClientError, ScholarUnavailable

#: OpenAlex's documented polite-pool ceiling (with `mailto=`). A configured
#: rate is clamped to this: exceeding it is what gets a deployment's mailto
#: address blocked, and that failure lands on every project at once.
POLITE_POOL_CEILING_PER_SECOND = 10.0

#: Default request rate: half the polite pool. Conservative by intent — the
#: number that unblocks the research loop's fan-out without approaching the
#: ceiling. Tune with `ALEPH_SCHOLAR_RATE_PER_SECOND`.
#:
#: MEASURED 2026-08-21, and it matters before anyone tunes this upward: against
#: the running deployment, OpenAlex answered 429 to a SINGLE request, and six of
#: eight concurrent searches, with no `Retry-After` header at all. So this
#: number was not what was throttling that deployment. The mailto was
#: `dev@aleph.local`, which is not a deliverable address — the polite pool is
#: granted on a contactable mailto, and a fake one leaves you in the common
#: pool. Setting a real `ALEPH_SCHOLAR_MAILTO` is the fix for those 429s; this
#: rate is the fix for Aleph queueing behind itself. They are different
#: problems and raising this one does not touch the other.
DEFAULT_RATE_PER_SECOND = 5.0

#: Burst allowance. A research `search` phase fans out in a clump and then
#: goes quiet; a burst of 10 lets one fan-out through at full speed while the
#: sustained rate still averages out to the configured limit.
DEFAULT_BURST = 10

#: Default per-request wall-clock budget, covering rate-limit waiting, every
#: attempt, and every backoff between them. Discovery search is best-effort:
#: 20s is long enough to ride out a short 429 and short enough that the loop
#: still finishes with whatever it got.
DEFAULT_DEADLINE_S = 20.0

#: Floor on the wait between attempts. A zero backoff turns a deadline into a
#: busy loop that re-issues the same failing request as fast as the token
#: bucket allows, which is exactly the behaviour a rate limit exists to stop.
_MIN_BACKOFF_S = 0.05

_ENV_RATE = "ALEPH_SCHOLAR_RATE_PER_SECOND"
_ENV_DEADLINE = "ALEPH_SCHOLAR_DEADLINE_S"

#: Body keys the two upstreams use for a human-readable error reason.
_REASON_KEYS = ("message", "error", "detail", "description")


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment, ignoring junk.

    A malformed value must not take the process down at import time: an
    unparseable rate limit is an operator typo, and falling back to the
    conservative default is strictly safer than refusing to boot.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _clamped_rate(rate: float) -> float:
    return min(max(rate, 0.01), POLITE_POOL_CEILING_PER_SECOND)


def _is_retryable_status(status: int) -> bool:
    """429 and 5xx are worth another attempt; every other 4xx is not."""
    return status == 429 or status >= 500


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a numeric `Retry-After` header. `None` for absent / HTTP-date form."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None  # HTTP-date form — fall back to exponential backoff
    return max(0.0, seconds)


def _reason(response: httpx.Response) -> str:
    """The upstream's own explanation, for echoing back to our caller.

    OpenAlex answers a bad filter with `{"error": ..., "message": ...}` and
    Crossref with a plain-text line; both are worth far more to whoever has to
    fix the query than "the upstream service is unavailable".
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        parts = [str(body[key]) for key in _REASON_KEYS if body.get(key)]
        if parts:
            return " — ".join(parts)
    try:
        return response.text.strip()
    except Exception:  # pragma: no cover — a body that cannot be decoded at all
        return ""


class _TokenBucket:
    """Per-host token bucket with a configurable rate and burst.

    `acquire` takes the caller's remaining budget and returns False rather
    than queueing past it. Without that, a low rate limit turned every
    concurrent caller into an unbounded wait: the request never failed, it
    just never happened, which is the worst of both answers.
    """

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

    async def acquire(self, *, max_wait: float) -> bool:
        async with self._lock:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                if wait > max_wait:
                    return False
                await self._sleep(wait)
                self._updated = self._clock()
                self._tokens = min(self._burst, 1.0)
            self._tokens -= 1.0
            return True


def ensure_ok(response: httpx.Response) -> httpx.Response:
    """Raise on any non-2xx the caller didn't handle, split by who is at fault.

    A 4xx means the request Aleph sent was rejected — `ScholarClientError`,
    carrying the upstream's reason so the API can report something a person
    can act on. A 429/5xx reaching here outlived the retry loop, so it is
    `ScholarUnavailable`.

    Neither is a crash: tri-state consumers fold both to `ok=None` (spec §1),
    and the reviewer's doi_verification node must never fail the review run.
    """
    status = response.status_code
    if status < 400:
        return response
    reason = _reason(response)
    where = f"{response.request.method} {response.request.url}"
    if _is_retryable_status(status):
        msg = f"scholar upstream returned HTTP {status} for {where}: {reason}"
        raise ScholarUnavailable(
            msg, status_code=status, retry_after=_retry_after_seconds(response)
        )
    msg = f"scholar upstream rejected {where} with HTTP {status}: {reason}"
    raise ScholarClientError(msg, status_code=status, reason=reason)


def _flight_key(url: str, params: dict[str, str] | None) -> str:
    """Identity of a GET for de-duplication: URL plus its sorted query."""
    if not params:
        return url
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{url}?{query}"


class ScholarHttp:
    """Shared httpx.AsyncClient wrapper with politeness + deadline + dedup."""

    def __init__(
        self,
        *,
        mailto: str,
        client: httpx.AsyncClient | None = None,
        rate_per_second: float | None = None,
        burst: int = DEFAULT_BURST,
        deadline_s: float | None = None,
        retry_wait_min: float = 1.0,
        retry_wait_max: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.mailto = mailto
        self.user_agent = f"aleph-scholar/0.1 (mailto:{mailto})"
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        # The composition root does not pass these yet, so the environment is
        # the only lever an operator has. Constructor argument wins when given.
        self.rate_per_second = _clamped_rate(
            rate_per_second
            if rate_per_second is not None
            else _env_float(_ENV_RATE, DEFAULT_RATE_PER_SECOND)
        )
        self.deadline_s = (
            deadline_s if deadline_s is not None else _env_float(_ENV_DEADLINE, DEFAULT_DEADLINE_S)
        )
        self._burst = burst
        self._retry_wait_min = retry_wait_min
        self._retry_wait_max = retry_wait_max
        self._clock = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._buckets: dict[str, _TokenBucket] = {}
        self._inflight: dict[str, asyncio.Future[httpx.Response]] = {}

    def _bucket(self, host: str) -> _TokenBucket:
        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = _TokenBucket(
                rate=self.rate_per_second,
                burst=self._burst,
                clock=self._clock,
                sleep=self._sleep,
            )
            self._buckets[host] = bucket
        return bucket

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        deadline_s: float | None = None,
    ) -> httpx.Response:
        """GET with politeness, single-flight de-duplication, and a deadline.

        Returns the response for any non-retryable status (404 included — the
        caller decides what "missing" means). Raises `ScholarUnavailable` when
        transport keeps failing, when 429/5xx persists past `deadline_s`, or
        when the rate limiter cannot admit the request inside that budget.

        **Single flight.** Concurrent GETs with the same URL and query share
        one upstream request and one response object. The research loop fans
        the same query out across sub-questions, so N identical searches used
        to cost N upstream requests *and* N token-bucket slots — the queue was
        mostly made of duplicates. Followers inherit the leader's deadline;
        their own is not applied, because the alternative is issuing the
        second request the de-duplication exists to avoid.
        """
        key = _flight_key(url, params)
        inflight = self._inflight.get(key)
        if inflight is not None:
            # shield: a follower giving up (its caller cancelled) must not
            # cancel the request every other follower is waiting on.
            return await asyncio.shield(inflight)

        budget = self.deadline_s if deadline_s is None else deadline_s
        task: asyncio.Future[httpx.Response] = asyncio.ensure_future(
            self._fetch(url, params, budget)
        )
        self._inflight[key] = task
        try:
            return await task
        finally:
            if self._inflight.get(key) is task:
                del self._inflight[key]

    async def _fetch(
        self, url: str, params: dict[str, str] | None, budget: float
    ) -> httpx.Response:
        """Attempt the GET until it succeeds, fails unretryably, or runs out of budget.

        There is no attempt ceiling on purpose. `stop_after_attempt(3)` meant
        the retry budget was three of *something whose duration nobody knows* —
        three fast 429s cost 2s, three slow timeouts cost 90s, and neither
        number was chosen. The loop below spends exactly `budget` seconds and
        reports what it last saw.
        """
        host = httpx.URL(url).host or ""
        deadline = self._clock() + budget
        backoff = max(self._retry_wait_min, _MIN_BACKOFF_S)
        attempts = 0
        last_status: int | None = None
        last_retry_after: float | None = None
        last_detail = "the deadline was spent before the first attempt"
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            if not await self._bucket(host).acquire(max_wait=remaining):
                last_detail = (
                    f"the {self.rate_per_second:g}/s rate limit could not admit the request "
                    f"within {remaining:.1f}s of remaining budget"
                )
                break
            attempts += 1
            try:
                response = await self._client.get(
                    url, params=params, headers={"User-Agent": self.user_agent}
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_status = None
                last_retry_after = None
                last_detail = repr(exc)
            else:
                if not _is_retryable_status(response.status_code):
                    return response
                last_status = response.status_code
                last_retry_after = _retry_after_seconds(response)
                last_detail = f"HTTP {response.status_code}"

            remaining = deadline - self._clock()
            wait = max(
                last_retry_after if last_retry_after is not None else backoff, _MIN_BACKOFF_S
            )
            if wait >= remaining:
                # Sleeping past the deadline buys nothing: report now, and hand
                # the upstream's own Retry-After up to the caller. Honoring a
                # 60s Retry-After inside a 20s budget was the old cap's job;
                # the budget does it now, without a second magic number.
                break
            await self._sleep(wait)
            backoff = min(backoff * 2, max(self._retry_wait_max, _MIN_BACKOFF_S))

        msg = (
            f"scholar upstream {host} unavailable after {attempts} attempt(s) "
            f"within a {budget:.1f}s budget: {last_detail}"
        )
        raise ScholarUnavailable(msg, status_code=last_status, retry_after=last_retry_after)

    async def aclose(self) -> None:
        await self._client.aclose()
