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
        mailto="test@aleph.local",
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
