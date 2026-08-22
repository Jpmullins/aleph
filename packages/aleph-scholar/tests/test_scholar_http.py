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
    EGRESS_BLOCK_RETRY_AFTER_S,
    POLITE_POOL_CEILING_PER_SECOND,
    ScholarHttp,
    egress_block_note,
)


def _http(
    handler,
    *,
    clock: FakeClock | None = None,
    burst: int = DEFAULT_BURST,
    rate_per_second: float | None = None,
    deadline_s: float | None = None,
    mailto: str = "scholar-tests@aleph-fixture.org",
):
    """A ScholarHttp over MockTransport whose clock advances only on sleep."""
    clock = clock or FakeClock()
    return ScholarHttp(
        mailto=mailto,
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
    assert seen == ["aleph-scholar/0.1 (mailto:scholar-tests@aleph-fixture.org)"]


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


# ---------------------------------------------------------------------------
# A Retry-After measured in hours is a block, not a rate limit (`WS-E2`)
# ---------------------------------------------------------------------------


async def test_a_multi_hour_retry_after_is_reported_as_a_block_not_a_backoff() -> None:
    """The 429 this deployment actually got, and what it now says about it.

    Measured against the running stack: `Retry-After: 12309`. The 503 body read
    "openalex did not answer within the request budget — retry in 12309s.",
    which is the wording for a transient blip, so two consecutive audits read it
    as one and looked for a rate-limit cause. There was none — the same request
    was 429 over IPv4 and 200 over IPv6.

    Asserted on what the sentence has to TELL somebody, not on its phrasing: the
    number, that waiting is not the remedy, and that the rate setting is not
    involved. A test that pinned the wording would go red on a comma and green
    on a sentence that said nothing.
    """
    clock = FakeClock()

    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12309"})

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(blocked, clock=clock, deadline_s=5.0).get("https://api.openalex.org/works")

    message = str(caught.value)
    assert "12309" in message
    assert "no request budget can wait it out" in message
    assert "ALEPH_SCHOLAR_RATE_PER_SECOND" in message
    # The header is still handed up untouched — the note explains it, it does
    # not replace it.
    assert caught.value.retry_after == 12309.0
    assert caught.value.status_code == 429


async def test_a_short_retry_after_gets_no_block_note() -> None:
    """A throttled burst must not be labelled a block.

    This is the half that keeps the note worth reading. A sentence attached to
    every 429 is a sentence people scroll past, and then the one 429 that IS a
    block looks exactly like the forty that were not.
    """
    clock = FakeClock()

    def throttled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "5"})

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(throttled, clock=clock, deadline_s=2.0).get("https://api.openalex.org/works")

    message = str(caught.value)
    assert "egress address" not in message
    assert "over IPv6" not in message
    assert caught.value.retry_after == 5.0


async def test_a_5xx_with_a_long_retry_after_is_not_called_a_block() -> None:
    """Only 429 carries the reading.

    A 503 with a long `Retry-After` is a maintenance window; the upstream is
    saying it is down, not that this caller is unwelcome. Telling an operator to
    go and look at the egress route would send them somewhere there is nothing
    to find.
    """
    clock = FakeClock()

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "12309"})

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(down, clock=clock, deadline_s=2.0).get("https://api.openalex.org/works")

    assert "egress address" not in str(caught.value)
    assert caught.value.status_code == 503


def test_the_block_threshold_is_a_boundary_not_a_coincidence() -> None:
    """Directly on `egress_block_note`, at the edge and either side of it.

    The three tests above drive the transport, which can only reach the note
    through a 429 that outlives a budget; that path cannot show where the line
    is. `EGRESS_BLOCK_RETRY_AFTER_S` is read from the module rather than
    written out here, because a test that restates the constant it tests only
    proves the constant equals itself — but the *behaviour* on each side of it
    is asserted, so moving the constant moves the boundary and both of these
    still mean something.
    """
    edge = EGRESS_BLOCK_RETRY_AFTER_S
    assert egress_block_note("api.openalex.org", edge) != ""
    assert egress_block_note("api.openalex.org", edge - 1) == ""
    assert egress_block_note("api.openalex.org", None) == ""
    # The host is named, so an operator reading a log with both upstreams in it
    # knows which one blocked them.
    assert "api.openalex.org" in egress_block_note("api.openalex.org", edge)
    assert "api.crossref.org" in egress_block_note("api.crossref.org", edge)


async def test_the_block_note_comes_before_the_mailto_note() -> None:
    """Ordering, with BOTH notes present — the design claim, finally asserted.

    This is the whole point of `egress_block_note`, and it had no test. Every
    other test in this file runs through `_http` with a CONTACTABLE mailto, so
    `self.degradation` is empty, the append loop executes at most once, and
    reversing the tuple to `(self.degradation, block)` leaves 136 tests green.
    Found by an adversarial pass running exactly that mutation.

    The ordering is not cosmetic. The two remedies are opposite — a throttled
    burst is fixed by slowing down, a blocked egress address is not fixed by
    anything this process can do — and "set a real mailto" became the accepted
    diagnosis for this 429 twice, in two audits, because the weaker
    explanation was the one in front of the reader.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "9035"})

    # An unset mailto, so the degradation note is non-empty and BOTH notes fire.
    http = _http(handler, mailto="", deadline_s=1.0)
    try:
        with pytest.raises(ScholarUnavailable) as caught:
            await http.get("https://api.openalex.org/works", params={})
    finally:
        await http.aclose()

    message = str(caught.value)
    block_at = message.find("9035s")
    mailto_at = message.find("ALEPH_SCHOLAR_MAILTO")
    assert block_at != -1, f"no egress-block note in: {message}"
    assert mailto_at != -1, f"no mailto note in: {message}"
    assert block_at < mailto_at, (
        "the mailto note is in front of the egress-block note. A reader stops at "
        f"the first explanation, and this one is wrong: {message}"
    )


def test_the_block_note_reports_hours_not_minutes() -> None:
    """`(2.5h)`, not `(150.6h)`. Also unpinned until an adversarial pass.

    `hours = retry_after / 3600.0` could be `/ 60.0` and every other assertion
    in this file still passes, because they all match on the seconds figure or
    on the host. An operator reads the parenthesis to decide whether to wait.
    """
    note = egress_block_note("api.openalex.org", 9035.0)
    assert "(2.5h)" in note, note
    assert "9035s" in note, note
