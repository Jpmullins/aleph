"""One metered door to the gateway.

"Weirdly rate limited" is what it looks like from outside; what it *is* is that
nothing in Aleph counted how many requests were in flight. Four independent
things fan out at once and none of them knew about the others:

* the assistant runs every tool call in a turn simultaneously, so six subagents
  can all be mid-call together;
* "Configure from gateway" probes **every** model the gateway advertises in one
  `asyncio.gather` (`aleph_models.autoconfigure`), which on a 40-model gateway
  is 40 simultaneous invocations;
* the compose healthcheck hits `/readyz` every 15 seconds forever, and that leg
  calls the gateway;
* ten worker jobs can each be mid-call.

Aleph connects OUT to whatever endpoint the operator has — a shared LiteLLM
key, a laptop running Ollama, a Bedrock proxy with a per-minute quota. Every one
of those has a ceiling, and until this module there was no place in the codebase
that had the concept of one.

**The door is a property of the endpoint, not of the caller.** `limiter_for()`
returns the *same* limiter for the same endpoint, so a call site that was never
handed a limiter still goes through the door rather than around it. That is
deliberate: `autoconfigure_bindings` passes an `httpx` client to `probe_model`
and knows nothing about limiting, and threading a limiter through every such
signature is exactly how one call site ends up unmetered while the sweep stays
green. It also lines up with per-project endpoints (`WS-MEP-4`): one door per
endpoint is what you want when there are several.

**Two ceilings, and only one of them has a safe default.**

* *Concurrency* bounds Aleph's own fan-out. A default is always right here,
  because the number describes Aleph, not the operator's quota.
* *Requests per minute* is a property of somebody's key, and Aleph cannot
  discover it. Inventing one would throttle a local endpoint that has no quota
  at all, so `rpm=0` (off) is the default and an operator turns it on. A limiter
  that quietly halves the throughput of a machine on your desk is a worse
  failure than no limiter, because nothing reports it.

A queued request is refused rather than parked forever: waiting longer than the
call itself would have taken is not politeness, it is a hang with no error
message, and this repository has already shipped one of those (45 `chunk_embed`
runs sat in `running` with no error recorded anywhere).
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog

from aleph_core.errors import GatewayUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_QUEUE_TIMEOUT_S",
    "DEFAULT_RPM",
    "GatewayLimiter",
    "LimitedTransport",
    "LimiterConfig",
    "LimiterStats",
    "close_gateway_clients",
    "configure_limits",
    "current_limits",
    "endpoint_key",
    "env_number",
    "limiter_for",
    "reset_limiters",
    "shared_gateway_client",
]

_log = structlog.get_logger(__name__)

#: Aleph's own fan-out ceiling. Sized from what Aleph does, not from what any
#: particular gateway allows: the assistant is an orchestrator plus six
#: subagents, so 8 lets one full turn through at once and still leaves a shared
#: virtual key far under the per-key limits LiteLLM deployments normally carry.
DEFAULT_MAX_CONCURRENCY = 8

#: OFF. See the module docstring — a per-minute quota belongs to the operator's
#: key and cannot be discovered, so guessing one throttles endpoints that have
#: no quota at all.
DEFAULT_RPM = 0

#: How long a request may wait for the door before it is refused. Matched to the
#: client's own 120s POST timeout: once a request has queued for longer than the
#: call would have taken, parking it further only converts a rate limit into an
#: unexplained hang.
DEFAULT_QUEUE_TIMEOUT_S = 120.0

_ENV_MAX_CONCURRENCY = "ALEPH_GATEWAY_MAX_CONCURRENCY"
_ENV_RPM = "ALEPH_GATEWAY_RPM"
_ENV_QUEUE_TIMEOUT = "ALEPH_GATEWAY_QUEUE_TIMEOUT_S"


def env_number(name: str, default: float, *, minimum: float = 0.0) -> float:
    """A number from the environment, ignoring junk.

    Public because `aleph_models.repricing` reads its own two intervals the
    same way, and two copies of "ignore an operator's typo rather than refusing
    to boot" is two chances for one of them to stop doing it.

    A malformed limit is an operator typo and must not take the process down at
    import time — the same reasoning as `aleph_scholar.http._env_float`, and the
    same conclusion: fall back to the conservative default.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        _log.warning("gateway.limit_unparseable", setting=name, value=raw, using=default)
        return default
    if value < minimum:
        _log.warning("gateway.limit_out_of_range", setting=name, value=value, using=default)
        return default
    return value


@dataclass(frozen=True)
class LimiterConfig:
    """The two ceilings and the queue budget, as one value.

    Frozen so a configuration can be handed around and compared. `from_settings`
    reads a Settings object structurally (`aleph-models` may not import an app's
    Settings class) and falls back to the environment, then to the defaults
    above.
    """

    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    #: Requests per minute. 0 disables the token bucket entirely.
    rpm: float = DEFAULT_RPM
    #: Bucket burst. Defaults to the concurrency ceiling, because admitting more
    #: at once than the semaphore allows through cannot help anybody.
    burst: int | None = None
    queue_timeout_s: float = DEFAULT_QUEUE_TIMEOUT_S

    @property
    def effective_burst(self) -> int:
        return self.burst if self.burst is not None else max(1, self.max_concurrency)

    @classmethod
    def from_settings(cls, settings: Any = None) -> LimiterConfig:
        """Read the limits from Settings if it carries them, else the environment.

        `getattr` rather than an attribute access on purpose. `aleph-models` is
        imported by two processes with two different Settings classes, and the
        fields do not exist on either one yet (`WS-P7` owns those files). A
        deployment can set `ALEPH_GATEWAY_MAX_CONCURRENCY` in the environment
        today, and the moment the field lands it wins — with no change here.
        """
        max_concurrency = getattr(settings, "aleph_gateway_max_concurrency", None)
        rpm = getattr(settings, "aleph_gateway_rpm", None)
        queue_timeout = getattr(settings, "aleph_gateway_queue_timeout_s", None)
        return cls(
            max_concurrency=int(
                max_concurrency
                if max_concurrency is not None
                else env_number(_ENV_MAX_CONCURRENCY, DEFAULT_MAX_CONCURRENCY, minimum=1)
            ),
            rpm=float(rpm if rpm is not None else env_number(_ENV_RPM, DEFAULT_RPM)),
            queue_timeout_s=float(
                queue_timeout
                if queue_timeout is not None
                else env_number(_ENV_QUEUE_TIMEOUT, DEFAULT_QUEUE_TIMEOUT_S, minimum=0.001)
            ),
        )


@dataclass
class LimiterStats:
    """What the door has seen. Read by tests and by operational reporting.

    `peak_in_flight` is the number that answers "is the ceiling doing anything",
    and it is kept here rather than derived, because a peak that is never
    recorded is indistinguishable from a peak of zero — the same shape as the
    `$0` cost and the empty chunk table this repository has already shipped.
    """

    admitted: int = 0
    refused: int = 0
    in_flight: int = 0
    peak_in_flight: int = 0
    waited_s: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "refused": self.refused,
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "waited_s": round(self.waited_s, 3),
        }


class _TokenBucket:
    """Rate limiting with a burst allowance, refusing rather than queueing past a deadline.

    Modelled on `aleph_scholar.http._TokenBucket`, deliberately re-implemented
    rather than imported: `aleph-scholar` carries zero workspace dependencies by
    rule, so importing it here would invert the DAG, and that bucket is welded
    to `ScholarUnavailable` and a per-request scholarly deadline.

    The absolute-deadline contract is copied exactly, because the bug it fixes is
    not obvious: a *relative* budget is re-based every time it is read, so the
    Nth queued caller waits behind N-1 sleeps that its own budget never saw and
    the limiter silently outlasts the deadline it was given.
    """

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._rate = rate_per_second
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, *, deadline: float) -> bool:
        async with self._lock:
            # Re-checked AFTER the queue: the wait that mattered may already
            # have happened in a queue this caller could not see.
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


class GatewayLimiter:
    """The door for one endpoint: a concurrency semaphore plus a token bucket.

    Every outbound request to that endpoint takes a slot for as long as it is
    actually in flight — which for a streamed response means until the body is
    finished, not until the headers arrive. Releasing on headers is the mistake
    that makes a limiter look like it works while an agent turn streaming for
    thirty seconds counts as zero.
    """

    def __init__(
        self,
        config: LimiterConfig | None = None,
        *,
        endpoint: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config or LimiterConfig()
        self.endpoint = endpoint
        self._clock = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._sem = asyncio.Semaphore(max(1, self.config.max_concurrency))
        self._bucket = (
            _TokenBucket(
                rate_per_second=self.config.rpm / 60.0,
                burst=self.config.effective_burst,
                clock=clock,
                sleep=self._sleep,
            )
            if self.config.rpm > 0
            else None
        )
        self.stats = LimiterStats()

    @property
    def max_concurrency(self) -> int:
        return max(1, self.config.max_concurrency)

    @asynccontextmanager
    async def slot(self, *, purpose: str = "") -> AsyncGenerator[None]:
        """Hold one slot for the duration of a request."""
        await self.acquire(purpose=purpose)
        try:
            yield
        finally:
            self.release()

    async def acquire(self, *, purpose: str = "") -> None:
        """Take a slot, or raise `GatewayUnavailable` rather than queue forever.

        The semaphore is taken first and the token second, so at most
        `max_concurrency` callers are ever waiting on the bucket. The order also
        means a bucket refusal has to hand the semaphore slot back — a leak
        there lowers the ceiling permanently and looks exactly like a gateway
        that got slower.
        """
        started = self._clock()
        deadline = started + self.config.queue_timeout_s
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=max(0.0, deadline - self._clock()))
        except TimeoutError:
            self.stats.refused += 1
            msg = (
                f"gateway {self.endpoint or 'endpoint'} is at its concurrency ceiling "
                f"({self.max_concurrency}); waited {self.config.queue_timeout_s:g}s for a slot"
            )
            raise GatewayUnavailable(msg) from None

        if self._bucket is not None:
            try:
                admitted = await self._bucket.acquire(deadline=deadline)
            except BaseException:
                self._sem.release()
                raise
            if not admitted:
                self._sem.release()
                self.stats.refused += 1
                msg = (
                    f"gateway {self.endpoint or 'endpoint'} rate limit ({self.config.rpm:g}/min) "
                    f"could not admit the request within {self.config.queue_timeout_s:g}s"
                )
                raise GatewayUnavailable(msg)

        waited = self._clock() - started
        self.stats.waited_s += waited
        self.stats.admitted += 1
        self.stats.in_flight += 1
        self.stats.peak_in_flight = max(self.stats.peak_in_flight, self.stats.in_flight)
        if waited > 1.0:
            # Worth a line: a request that queued for a second is the operator's
            # first evidence that the ceiling is the binding constraint.
            _log.info(
                "gateway.limiter_waited",
                endpoint=self.endpoint,
                purpose=purpose,
                waited_s=round(waited, 3),
                ceiling=self.max_concurrency,
            )

    def release(self) -> None:
        self.stats.in_flight = max(0, self.stats.in_flight - 1)
        self._sem.release()

    def snapshot(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "max_concurrency": self.max_concurrency,
            "rpm": self.config.rpm,
            **self.stats.snapshot(),
        }


# ---------------------------------------------------------------------------
# The registry: one door per endpoint
# ---------------------------------------------------------------------------


def endpoint_key(base_url: str) -> str:
    """Normalise a gateway URL to the identity of the *server* behind it.

    The same endpoint is spelled two ways in this codebase: `LiteLLMClient` is
    built with `https://gw.example.com` and appends `/v1/chat/completions`,
    while the agent's `ChatOpenAI` is built with `https://gw.example.com/v1`
    because the OpenAI SDK appends `/chat/completions`. Keying on the raw string
    would give one server two doors, and two doors is the same as none.
    """
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        url = httpx.URL(raw)
    except (ValueError, TypeError, httpx.InvalidURL):
        return raw.lower()
    path = url.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    host = url.netloc.decode("ascii", "ignore").lower()
    return f"{url.scheme.lower()}://{host}{path}"


_default_config = LimiterConfig()
_limiters: dict[str, GatewayLimiter] = {}
_clients: dict[str, httpx.AsyncClient] = {}


def configure_limits(config: LimiterConfig) -> None:
    """Set the limits new doors are built with. **Call once, at boot.**

    Existing doors are discarded rather than resized: an `asyncio.Semaphore`
    cannot change its ceiling without either stranding waiters or briefly
    admitting more than the new limit. At boot there is no traffic, so
    discarding is free; calling this under load is not supported and would
    momentarily exceed the ceiling it is setting.
    """
    global _default_config
    _default_config = config
    _limiters.clear()


def current_limits() -> LimiterConfig:
    return _default_config


def limiter_for(base_url: str, *, config: LimiterConfig | None = None) -> GatewayLimiter:
    """The door for `base_url`, created on first use and shared thereafter."""
    key = endpoint_key(base_url)
    existing = _limiters.get(key)
    if existing is not None:
        return existing
    limiter = GatewayLimiter(config or _default_config, endpoint=key)
    _limiters[key] = limiter
    return limiter


def reset_limiters() -> None:
    """Forget every door and restore the shipped defaults.

    Boot-time inverse and test hygiene. Deliberately does NOT close the shared
    clients — that needs a running loop, so it is `close_gateway_clients`.
    """
    global _default_config
    _default_config = LimiterConfig()
    _limiters.clear()


def shared_gateway_client(
    base_url: str,
    *,
    timeout: float | httpx.Timeout = 120.0,
    limiter: GatewayLimiter | None = None,
) -> httpx.AsyncClient:
    """One limiter-aware `httpx.AsyncClient` per endpoint, for callers that own their own.

    Exists for `ChatOpenAI`, which builds its own HTTP client and is therefore
    the one place a limiter cannot sit inside Aleph's own transport code. Pass
    this as `http_async_client=` and the agent's traffic goes through the same
    door as everything else.

    Shared per endpoint rather than per model on purpose: the assistant builds
    seven `ChatOpenAI` instances (orchestrator plus six subagents), and seven
    private connection pools is the very shape `WS-MEP-4` warns about — N
    clients each with their own unbounded pool.
    """
    key = endpoint_key(base_url)
    existing = _clients.get(key)
    if existing is not None and not existing.is_closed:
        return existing
    client = httpx.AsyncClient(
        transport=LimitedTransport(limiter or limiter_for(base_url)),
        timeout=timeout,
    )
    _clients[key] = client
    return client


async def close_gateway_clients() -> None:
    """Close every client `shared_gateway_client` handed out. Boot-time inverse."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        if not client.is_closed:
            await client.aclose()


# ---------------------------------------------------------------------------
# The transport seam
# ---------------------------------------------------------------------------


class _ReleaseOnClose(httpx.AsyncByteStream):
    """Wraps a response body so the slot is held until the body is finished.

    A streamed completion returns headers immediately and then trickles tokens
    for as long as the model talks. Releasing when `handle_async_request`
    returns would count a thirty-second agent turn as an instant, and the
    ceiling would bound nothing that matters.

    `release` is idempotent because httpx may close a stream more than once
    (an explicit `aclose()` inside a `finally`, plus the client's own cleanup),
    and a double release raises the semaphore's ceiling for good.
    """

    def __init__(self, stream: httpx.AsyncByteStream, release: Callable[[], None]) -> None:
        self._stream = stream
        self._release = release
        self._released = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                yield chunk
        except BaseException:
            self._release_once()
            raise

    async def aclose(self) -> None:
        try:
            aclose = getattr(self._stream, "aclose", None)
            if aclose is not None:
                await aclose()
        finally:
            self._release_once()

    def _release_once(self) -> None:
        if self._released:
            return
        self._released = True
        self._release()


class LimitedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that holds a limiter slot for the life of the request.

    The only seam where a limiter can sit under `ChatOpenAI` without forking
    langchain: the OpenAI SDK owns the request, but not the transport it is
    handed.
    """

    def __init__(
        self,
        limiter: GatewayLimiter,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        purpose: str = "agent",
    ) -> None:
        self._limiter = limiter
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._purpose = purpose

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._limiter.acquire(purpose=self._purpose)
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException:
            self._limiter.release()
            raise
        # `Response.stream` is typed as either byte stream; a transport only
        # ever builds the async one.
        response.stream = _ReleaseOnClose(
            cast("httpx.AsyncByteStream", response.stream), self._limiter.release
        )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
