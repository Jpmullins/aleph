"""The retry budget is wall-clock seconds the caller chose, not three attempts.

`stop_after_attempt(3)` is a budget of three of *something whose duration
nobody knows*: three fast 429s cost two seconds, three connect timeouts cost
ninety, and neither number was chosen by anyone. Worse, the rate limiter sat
outside the budget entirely — a request that could not get a token simply
waited, with no bound at all, so a low limit produced a hang rather than an
error. Both are budgeted here.

Every test runs on `FakeClock`, which advances only when the transport's sleep
is awaited, so "the budget ran out" is exact and instant.
"""

from __future__ import annotations

import httpx
import pytest
from scholar_test_support import FakeClock

from aleph_scholar.errors import ScholarUnavailable
from aleph_scholar.http import DEFAULT_DEADLINE_S, ScholarHttp

_URL = "https://api.openalex.org/works"


def _http(
    handler,
    *,
    clock: FakeClock,
    deadline_s: float | None = None,
    rate_per_second: float | None = None,
    burst: int = 5,
    retry_wait_min: float = 0.0,
    retry_wait_max: float = 0.0,
) -> ScholarHttp:
    return ScholarHttp(
        mailto="scholar-tests@aleph-fixture.org",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_wait_min=retry_wait_min,
        retry_wait_max=retry_wait_max,
        burst=burst,
        rate_per_second=rate_per_second,
        deadline_s=deadline_s,
        clock=clock,
        sleep=clock.sleep,
    )


def _always(status: int, **headers: str):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, headers=headers or None)

    return handler, calls


async def test_the_budget_is_seconds_not_attempts() -> None:
    """A longer budget buys more attempts; the old cap of 3 was fixed."""
    clock_short = FakeClock()
    handler, short_calls = _always(503)
    with pytest.raises(ScholarUnavailable):
        await _http(handler, clock=clock_short, deadline_s=0.5).get(_URL)

    clock_long = FakeClock()
    handler_long, long_calls = _always(503)
    with pytest.raises(ScholarUnavailable):
        await _http(handler_long, clock=clock_long, deadline_s=5.0).get(_URL)

    assert len(short_calls) > 3  # not capped at three
    assert len(long_calls) > len(short_calls)  # the budget, not a constant, decides
    assert clock_short.elapsed <= 0.5
    assert clock_long.elapsed <= 5.0


async def test_the_caller_chooses_the_budget_per_request() -> None:
    """A per-request deadline overrides the instance default."""
    clock = FakeClock()
    handler, calls = _always(503)
    http = _http(handler, clock=clock, deadline_s=30.0)

    with pytest.raises(ScholarUnavailable):
        await http.get(_URL, deadline_s=0.2)

    assert clock.elapsed <= 0.2  # the argument won, not the 30s default
    assert len(calls) >= 1


async def test_the_error_names_the_budget_and_what_it_last_saw() -> None:
    """An unactionable error message is the defect this workstream is about."""
    clock = FakeClock()
    handler, _ = _always(503)
    # A burst wide enough that the budget, not the rate limit, is what runs
    # out — the two produce different (both correct) messages.
    with pytest.raises(ScholarUnavailable) as caught:
        await _http(handler, clock=clock, deadline_s=0.3, burst=50).get(_URL)

    message = str(caught.value)
    assert "api.openalex.org" in message
    assert "0.3s budget" in message
    assert "HTTP 503" in message
    assert "attempt(s)" in message


async def test_exponential_backoff_between_attempts() -> None:
    clock = FakeClock()
    handler, _ = _always(500)
    http = _http(handler, clock=clock, deadline_s=30.0, retry_wait_min=1.0, retry_wait_max=4.0)

    with pytest.raises(ScholarUnavailable):
        await http.get(_URL)

    assert clock.sleeps[:4] == [1.0, 2.0, 4.0, 4.0]  # doubling, then capped


async def test_the_rate_limiter_cannot_outlast_the_deadline() -> None:
    """A throttled request fails on its deadline instead of queueing forever.

    This is the hang half of the reported "weirdly rate limited" behaviour: the
    token bucket's wait was unbounded, so a request that could not get a token
    inside any useful time neither succeeded nor failed — it just never
    happened, which is the one answer a caller cannot do anything with.
    """
    clock = FakeClock()
    handler, calls = _always(200)
    # 0.01 req/s = 100 seconds per token, burst spent, budget of 1 second.
    http = _http(handler, clock=clock, rate_per_second=0.01, burst=1, deadline_s=1.0)

    assert (await http.get(_URL, params={"search": "first"})).status_code == 200  # spends the burst

    with pytest.raises(ScholarUnavailable) as caught:
        await http.get(_URL, params={"search": "second"})

    assert "rate limit could not admit the request" in str(caught.value)
    assert len(calls) == 1  # the second request was never sent
    assert clock.elapsed == 0.0  # and it did not wait 100 seconds to say so


async def test_a_deadline_is_not_a_timeout_on_a_healthy_request() -> None:
    """The budget covers waiting and retrying, not a successful round trip."""
    clock = FakeClock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"results": []})

    response = await _http(handler, clock=clock, deadline_s=0.05).get(_URL)
    assert response.status_code == 200
    assert len(calls) == 1


async def test_the_default_budget_is_conservative_and_env_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    clock = FakeClock()
    assert _http(handler, clock=clock).deadline_s == DEFAULT_DEADLINE_S

    monkeypatch.setenv("ALEPH_SCHOLAR_DEADLINE_S", "7.5")
    assert _http(handler, clock=clock).deadline_s == 7.5

    monkeypatch.setenv("ALEPH_SCHOLAR_DEADLINE_S", "nonsense")
    assert _http(handler, clock=clock).deadline_s == DEFAULT_DEADLINE_S


# ---------------------------------------------------------------------------
# The deadline has to cover time spent QUEUEING, not just time spent sleeping
#
# `_TokenBucket.acquire` held its lock across the sleep and computed the wait
# once, before queueing for that lock. So the Nth concurrent caller queued
# behind N-1 sleeps its own budget never saw. Measured against the real class
# with the real clock: eight concurrent callers at rate 1/s, burst 1, each with
# a 2.0s deadline, ALL returned True after 7.0 seconds — every one of them three
# and a half times past the budget it had declared, inside the component whose
# stated job is to refuse rather than queue past it.
# ---------------------------------------------------------------------------


async def test_a_caller_is_refused_when_the_queue_ate_its_budget() -> None:
    from aleph_scholar.http import _TokenBucket

    clock = FakeClock()
    bucket = _TokenBucket(rate=1.0, burst=1, clock=clock, sleep=clock.sleep)

    # Eight callers that all started at the same instant and are each willing to
    # wait 2s. ONE shared absolute deadline is what "concurrent" means here — and
    # it is the thing a relative budget could not express, because each caller
    # re-based it after the previous one had already slept.
    deadline = clock() + 2.0
    results = [await bucket.acquire(deadline=deadline) for _ in range(8)]

    admitted = sum(1 for r in results if r)
    assert admitted < 8, (
        "every caller was admitted, so the limiter queued them past the budget each one declared"
    )
    # The first has the token; the next two can wait 1s and 2s inside 2s. Beyond
    # that the queue itself has consumed the budget.
    assert admitted <= 3, f"{admitted} callers were admitted on a 2s budget at 1/s"


async def test_the_refusal_is_not_just_a_low_ceiling() -> None:
    """The check must be able to say yes. A limiter that refuses everything
    under contention is a different bug with the same test."""
    from aleph_scholar.http import _TokenBucket

    clock = FakeClock()
    bucket = _TokenBucket(rate=1.0, burst=8, clock=clock, sleep=clock.sleep)

    deadline = clock() + 2.0
    results = [await bucket.acquire(deadline=deadline) for _ in range(8)]
    assert all(results), "a burst of eight should admit eight without waiting at all"
    assert clock.elapsed == 0.0


async def test_a_single_caller_still_waits_the_time_it_agreed_to() -> None:
    """Uncontended, the behaviour is unchanged: wait up to the budget, then go."""
    from aleph_scholar.http import _TokenBucket

    clock = FakeClock()
    bucket = _TokenBucket(rate=1.0, burst=1, clock=clock, sleep=clock.sleep)

    assert await bucket.acquire(deadline=clock() + 5.0) is True  # takes the only token
    assert await bucket.acquire(deadline=clock() + 5.0) is True  # waits ~1s for the next
    assert clock.elapsed == pytest.approx(1.0)
