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

**What the measured 429s actually were, 2026-08-22.** The comment that used to
sit on `DEFAULT_RATE_PER_SECOND` said an undeliverable `ALEPH_SCHOLAR_MAILTO`
was why this deployment got 429s, and the audit repeated it. It is wrong. Same
URL, same User-Agent, same `mailto=`, same host, differing only in address
family:

    curl -4 …/works?search=long+context&mailto=dev@aleph.local
        -> 429  remote=172.66.159.136          Retry-After: 25668
    curl -6 …/works?search=long+context&mailto=dev@aleph.local
        -> 200  remote=2606:4700:10::ac42:9f88

Dropping the mailto and the User-Agent entirely changed nothing: `-4` still 429.
Successive IPv4 calls returned 25667, 25665, 25663 — one fixed wall-clock
deadline counting down on the ADDRESS, about 7.1 hours out. Crossref over the
same IPv4 answered 200, so it is not the network and not this host generally.

OpenAlex is blocking this deployment's **IPv4 egress**. The host escapes it only
because it prefers IPv6; the API container is IPv4-only, which is exactly why
the fan-out probe scored 0/8 from inside the container while the identical curl
from the host scored 200. The remedy is IPv6 egress for the container, which is
a compose concern, not anything in this module.

A contactable mailto is still worth having — it is what the polite pool is
granted on, and `is_contactable` below stops this client claiming one it does
not have — but it is not what those 429s were, and this module must not say it
is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from aleph_scholar.errors import ScholarClientError, ScholarUnavailable

_log = logging.getLogger(__name__)

#: OpenAlex's documented polite-pool ceiling (with `mailto=`). A configured
#: rate is clamped to this: exceeding it is what gets a deployment's mailto
#: address blocked, and that failure lands on every project at once.
POLITE_POOL_CEILING_PER_SECOND = 10.0

#: The ceiling applied when the configured mailto is NOT a contactable address.
#: A number Aleph chose, not one any upstream documents — which is the point:
#: with no reachable contact there is no polite pool, so clamping to the POLITE
#: ceiling clamps to an allowance this deployment was never granted. One request
#: per second is the conservative side of a limit nobody has published.
#:
#: This is an entitlement correction, NOT a fix for the 429s measured on this
#: deployment. See the module docstring, *What the measured 429s actually were*.
COMMON_POOL_CEILING_PER_SECOND = 1.0


#: Domains and suffixes that cannot receive mail, so an address on one of them
#: is a placeholder however well-formed it looks. `.local` is mDNS (RFC 6762);
#: `.test`, `.example`, `.invalid` and `.localhost` are reserved by RFC 2606 /
#: RFC 6761; the `example.*` second-level domains are reserved by RFC 2606 §3.
#: `.internal` is ICANN-reserved for private use.
_UNDELIVERABLE_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".localhost",
    ".invalid",
    ".test",
    ".example",
    ".internal",
)
_UNDELIVERABLE_DOMAINS: frozenset[str] = frozenset(
    {"example.com", "example.org", "example.net", "localhost"}
)

_ENV_MAILTO = "ALEPH_SCHOLAR_MAILTO"

#: Default request rate: half the polite pool. Conservative by intent — the
#: number that unblocks the research loop's fan-out without approaching the
#: ceiling. Tune with `ALEPH_SCHOLAR_RATE_PER_SECOND`.
#:
#: MEASURED 2026-08-21, and it matters before anyone tunes this upward: against
#: the running deployment, OpenAlex answered 429 to a SINGLE request, and six of
#: eight concurrent searches. So this number was not what was throttling that
#: deployment.
#:
#: An earlier version of this comment said the fix was a real
#: `ALEPH_SCHOLAR_MAILTO`. RETRACTED 2026-08-22: an A/B differing only in IP
#: address family reproduces the 429 with and without a mailto over IPv4 and
#: cannot reproduce it at all over IPv6 — see the module docstring.
#: A contactable mailto is still worth setting — it is what the polite pool is
#: granted on — but it is not what those 429s were.
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


def is_contactable(mailto: str) -> bool:
    """Whether `mailto` is an address a human upstream could actually reach.

    Not validation — a deliverable-looking address can still bounce, and this
    makes no attempt to find out. It answers the one question the polite pool
    turns on: is this a *placeholder*. `dev@aleph.local`, which Aleph ships as
    its default, is well formed and undeliverable, and the polite pool is
    granted on a contactable address. So the deployment sent `mailto=` on every
    request, clamped its rate to the polite ceiling, and had been granted
    neither — with nothing anywhere reporting the gap.

    That gap is worth closing on its own terms. It is NOT what caused the 429s
    measured on this deployment; see the module docstring.
    """
    address = mailto.strip().lower()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        return False
    domain = address.rsplit("@", 1)[1]
    if not domain or "." not in domain:
        return False
    if domain in _UNDELIVERABLE_DOMAINS:
        return False
    return not domain.endswith(_UNDELIVERABLE_SUFFIXES)


def _clamped_rate(rate: float, ceiling: float) -> float:
    return min(max(rate, 0.01), ceiling)


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

    async def acquire(self, *, deadline: float) -> bool:
        """Take a token, or return False rather than wait past ``deadline``.

        ``deadline`` is an ABSOLUTE clock value, not a relative budget, and that
        is the whole fix.

        A relative ``max_wait`` was re-based every time it was read, so the Nth
        concurrent caller queued behind N-1 sleeps its own budget never saw.
        Measured with the real class: eight concurrent callers at rate 1/s,
        burst 1, each with a 2.0s deadline, all returned True after 7.0s — every
        one of them three and a half times past the budget it had declared,
        inside the component whose stated job is to refuse rather than queue
        past it.

        An absolute deadline cannot be re-based. The caller fixes it once from
        its own remaining budget, and it is re-checked after the lock is
        acquired, so a caller that spent its budget waiting for the lock is
        refused there rather than going on to sleep for a token as well.
        """
        async with self._lock:
            # Re-check AFTER queueing. The wait that mattered may already have
            # happened, in a queue this caller could not see.
            remaining = deadline - self._clock()
            if remaining < 0:
                return False
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                if wait > remaining:
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
        #: Whether this instance is entitled to the polite pool at all. Every
        #: politeness behaviour below reads it rather than assuming.
        self.polite = is_contactable(mailto)
        #: Empty when polite; otherwise one sentence naming the cause and the
        #: fix. It is appended to the `ScholarUnavailable` message on a
        #: throttled request, which is how it reaches the operator: the API
        #: route echoes that message into the 503 body, so a rate-limit failure
        #: says WHY instead of "the upstream is unavailable".
        self.degradation = (
            ""
            if self.polite
            else (
                f"{_ENV_MAILTO} is {mailto!r}, which is not a deliverable address, "
                "so this deployment has not been granted the polite pool and is "
                f"self-limited to {COMMON_POOL_CEILING_PER_SECOND:g}/s. Worth "
                "checking, not a diagnosis: a 429 can equally be a block on the "
                f"egress address. Set {_ENV_MAILTO} to a real contact address."
            )
        )
        # A placeholder contact is not claimed as a real one. Sending
        # `(mailto:dev@aleph.local)` asserts a contact that does not exist,
        # which is what the polite pool is a promise about; the software still
        # identifies itself.
        self.user_agent = (
            f"aleph-scholar/0.1 (mailto:{mailto})" if self.polite else "aleph-scholar/0.1"
        )
        if not self.polite:
            # Once per client, not once per request: the point is that an
            # operator reading the API log at startup sees the cause of the
            # 429s, not that every throttled call repeats it.
            _log.warning("scholar.mailto_not_contactable: %s", self.degradation)
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        # The composition root does not pass these yet, so the environment is
        # the only lever an operator has. Constructor argument wins when given.
        #
        # The CEILING depends on politeness, and that is the substantive half of
        # the degradation: without a contactable mailto there is no polite pool,
        # so clamping a configured 5/s to the polite ceiling clamps it to a
        # budget this deployment was never granted.
        self.rate_per_second = _clamped_rate(
            rate_per_second
            if rate_per_second is not None
            else _env_float(_ENV_RATE, DEFAULT_RATE_PER_SECOND),
            POLITE_POOL_CEILING_PER_SECOND if self.polite else COMMON_POOL_CEILING_PER_SECOND,
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

    def mailto_params(self) -> dict[str, str]:
        """The `mailto=` query parameter, when there is a real address to send.

        OpenAlex reads `mailto=` to decide pool membership. Sending a
        placeholder does not buy the polite pool and does assert a contact
        nobody can use, so an uncontactable address is omitted rather than
        transmitted — the honest request for a deployment that has not been
        configured.
        """
        return {"mailto": self.mailto} if self.polite else {}

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
        one upstream request and one response object, so N identical searches
        cost one upstream request and one token-bucket slot rather than N of
        each. Followers inherit the leader's deadline; their own is not applied,
        because the alternative is issuing the second request the
        de-duplication exists to avoid.

        The consumer is concurrent HTTP traffic: `POST /v1/scholar/search`
        fanned out by a UI or by several analysts converging on the same
        question. It is deliberately NOT justified by the research loop —
        `research_workflow._node_search` is a sequential double `for` loop with
        no `gather` or `TaskGroup`, so it issues no concurrent duplicates at
        all, and an earlier version of this docstring claimed otherwise.
        De-duplication is per-in-flight and not a cache, so sequential repeats
        still go upstream, by design.
        """
        key = _flight_key(url, params)
        inflight = self._inflight.get(key)
        if inflight is not None:
            # shield: a waiter giving up (its caller cancelled) must not cancel
            # the request every other waiter is on.
            return await asyncio.shield(inflight)

        budget = self.deadline_s if deadline_s is None else deadline_s
        task: asyncio.Future[httpx.Response] = asyncio.ensure_future(
            self._fetch(url, params, budget)
        )
        self._inflight[key] = task
        # The LEADER shields too, and this is not symmetry for its own sake.
        # `await task` propagates the awaiter's cancellation INTO the task, so a
        # leader whose caller disconnected — which is exactly what FastAPI does
        # to a request task when the client goes away — killed the flight and
        # every follower waiting on it. Measured: three identical concurrent
        # GETs, cancel the leader, all three raise CancelledError. That is a
        # failure mode the pre-de-duplication code could not have had, so the
        # optimisation would have made one disconnecting client break other
        # people's searches.
        #
        # The key is cleared on the TASK's completion rather than in a `finally`
        # here, because a cancelled leader's `finally` runs while the flight is
        # still in progress — clearing it there would let the next caller start
        # the second request de-duplication exists to avoid.
        task.add_done_callback(lambda done: self._forget(key, done))
        return await asyncio.shield(task)

    def _forget(self, key: str, task: asyncio.Future[httpx.Response]) -> None:
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
            # The absolute deadline, not `remaining`: re-deriving a relative
            # budget at each call site is how the limiter came to outlast it.
            if not await self._bucket(host).acquire(deadline=deadline):
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
        # A 429 against a deployment that never had the polite pool is worth one
        # sentence naming that fact, because otherwise the only artefact is a
        # 503 that reads as an outage. It is offered as something to check, not
        # as the cause — on THIS deployment the cause turned out to be an
        # OpenAlex block on the IPv4 egress address, unaffected by the mailto
        # (see the module docstring). Appended only on 429: a 500 or a
        # timeout says nothing about pool membership, and attaching the note
        # there would train people to scroll past it.
        if last_status == 429 and self.degradation:
            msg = f"{msg}. {self.degradation}"
        raise ScholarUnavailable(msg, status_code=last_status, retry_after=last_retry_after)

    async def aclose(self) -> None:
        await self._client.aclose()
