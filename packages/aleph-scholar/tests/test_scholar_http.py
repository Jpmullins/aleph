"""ScholarHttp: user agent, retry policy, Retry-After, token bucket."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from scholar_test_support import FakeClock

from aleph_scholar.errors import ScholarUnavailable, ScholarUpstreamError
from aleph_scholar.http import (
    DEFAULT_BURST,
    DEFAULT_RATE_PER_SECOND,
    POLITE_POOL_CEILING_PER_SECOND,
    ScholarHttp,
)


def _http(
    handler,
    *,
    clock: FakeClock | None = None,
    burst: int = DEFAULT_BURST,
    rate_per_second: float | None = None,
    deadline_s: float | None = None,
):
    """A ScholarHttp over MockTransport whose clock advances only on sleep."""
    clock = clock or FakeClock()
    return ScholarHttp(
        mailto="test@aleph.local",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_wait_min=0.0,
        retry_wait_max=0.0,
        burst=burst,
        rate_per_second=rate_per_second,
        deadline_s=deadline_s,
        clock=clock,
        sleep=clock.sleep,
    )


async def test_sends_polite_user_agent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={})

    await _http(handler).get("https://api.crossref.org/works/10.1/x")
    assert seen == ["aleph-scholar/0.1 (mailto:test@aleph.local)"]


async def test_retries_5xx_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    response = await _http(handler).get("https://api.openalex.org/works")
    assert response.status_code == 200
    assert len(calls) == 3


async def test_exhausted_retries_raise_scholar_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(ScholarUpstreamError):
        await _http(handler, deadline_s=1.0).get("https://api.openalex.org/works")


async def test_transport_error_raises_scholar_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ScholarUpstreamError):
        await _http(handler, deadline_s=1.0).get("https://api.openalex.org/works")


async def test_404_is_returned_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Resource not found.")

    response = await _http(handler).get("https://api.crossref.org/works/10.1/fake")
    assert response.status_code == 404


async def test_retry_after_is_honored_and_bounded_by_the_deadline() -> None:
    """The upstream's own Retry-After is slept, but never past the budget.

    The old code clamped Retry-After at a flat 8s constant, which was a second
    magic number pulled from nowhere: it could still sleep 8s inside a request
    the caller was about to abandon, and it silently ignored a legitimate
    longer wait. The deadline is now the only bound.
    """
    clock = FakeClock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={})

    response = await _http(handler, clock=clock, deadline_s=10.0).get(
        "https://api.openalex.org/works"
    )
    assert response.status_code == 200
    assert clock.sleeps == [3.0]  # honored in full — it fits inside 10s

    # Same header, a budget it does not fit into: no sleep, fail immediately.
    clock2 = FakeClock()
    calls.clear()

    def always_429(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "3"})

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(always_429, clock=clock2, deadline_s=2.0).get("https://api.openalex.org/works")
    assert clock2.sleeps == []
    assert len(calls) == 1
    assert caught.value.retry_after == 3.0  # handed up, not swallowed


async def test_token_bucket_throttles_after_burst() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    http = _http(handler, clock=clock)
    for _ in range(DEFAULT_BURST):
        await http.get(f"https://api.openalex.org/works/{_}")
    assert clock.sleeps == []  # the burst goes through without waiting
    await http.get("https://api.openalex.org/works/last")
    # 0.2s: one token at 5 req/s. Written as a literal, not as
    # `1 / DEFAULT_RATE_PER_SECOND` — deriving the expectation from the constant
    # under test makes the assertion agree with any value the constant takes,
    # which is how a check ends up unable to fail. Verified: it goes red when
    # the default is put back to 1 req/s.
    assert clock.sleeps == [pytest.approx(0.2)]


async def test_the_default_rate_is_conservative_but_not_serialising() -> None:
    """5 req/s: half the polite pool, and enough that a fan-out does not queue.

    The two halves of this are a real trade-off, so both are pinned. Too high
    and the deployment's mailto address gets blocked, which takes every project
    down at once. Too low — 1 req/s, as shipped — and the research loop's
    `search` phase, which fans out one query per sub-question, spends seconds
    waiting on its own rate limiter before any upstream latency at all.
    """
    assert DEFAULT_RATE_PER_SECOND == 5.0
    assert DEFAULT_RATE_PER_SECOND <= POLITE_POOL_CEILING_PER_SECOND / 2
    assert DEFAULT_BURST == 10


async def test_a_fan_out_of_eight_does_not_queue_on_the_rate_limiter() -> None:
    """Eight distinct concurrent searches spend zero time throttled.

    This is the throughput criterion stated as behaviour rather than as a
    constant. At the shipped 1 req/s with a burst of 5 the sixth, seventh and
    eighth searches each waited a second before being sent — and each then had
    its own retries to get through, which is the "weirdly rate limited" fan-out
    in the backlog and the reason searches timed out rather than failed.

    Distinct queries on purpose: identical ones now collapse into a single
    flight, so eight copies of one query would measure the de-duplication and
    say nothing about the rate limit.
    """
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    http = _http(handler, clock=clock)
    await asyncio.gather(
        *(http.get("https://api.openalex.org/works", params={"search": f"q{i}"}) for i in range(8))
    )

    assert clock.sleeps == []
    assert clock.elapsed == 0.0


async def test_token_bucket_is_per_host() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    http = _http(handler, clock=clock, burst=1)
    await http.get("https://api.openalex.org/works")
    await http.get("https://api.crossref.org/works")  # different host, fresh bucket
    assert clock.sleeps == []


async def test_configured_rate_cannot_exceed_the_polite_pool() -> None:
    """A configured rate is clamped, not obeyed.

    Exceeding OpenAlex's polite pool gets the deployment's mailto address
    blocked, and that lands on every project at once — so an operator typo (or
    an optimistic tuning session) must not be able to cause it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    assert _http(handler, rate_per_second=100.0).rate_per_second == POLITE_POOL_CEILING_PER_SECOND
    assert _http(handler, rate_per_second=2.0).rate_per_second == 2.0
    assert _http(handler).rate_per_second == DEFAULT_RATE_PER_SECOND


async def test_rate_limit_is_configurable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition root does not pass a rate yet, so the env is the lever."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    monkeypatch.setenv("ALEPH_SCHOLAR_RATE_PER_SECOND", "3")
    assert _http(handler).rate_per_second == 3.0

    monkeypatch.setenv("ALEPH_SCHOLAR_RATE_PER_SECOND", "not-a-number")
    assert _http(handler).rate_per_second == DEFAULT_RATE_PER_SECOND  # typo → safe default

    monkeypatch.setenv("ALEPH_SCHOLAR_RATE_PER_SECOND", "99")
    assert _http(handler).rate_per_second == POLITE_POOL_CEILING_PER_SECOND  # still clamped
