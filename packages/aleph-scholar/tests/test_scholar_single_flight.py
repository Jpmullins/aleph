"""Identical concurrent GETs share one upstream request.

The throughput defect this file pins: the research loop fans one question out
across sub-questions, and those sub-questions repeat queries. With a per-host
token bucket and no de-duplication, N identical concurrent searches cost N
upstream requests *and* N slots in the rate-limit queue — so the queue that
made every search wait was mostly made of requests for an answer already in
flight. Eight identical queries at 1 req/s meant seven of them waited on
duplicates of themselves.

De-duplication is per in-flight request, not a cache: once a flight finishes,
the next identical query goes upstream again. Nothing here makes a stale
answer possible.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from scholar_test_support import FakeClock

from aleph_scholar.errors import ScholarUnavailable
from aleph_scholar.http import ScholarHttp

_URL = "https://api.openalex.org/works"
_FANOUT = 8


class GatedUpstream:
    """A MockTransport handler that holds every request until released.

    Gating is what makes "concurrent" a fact rather than a hope: without it the
    assertion would depend on how the event loop happens to interleave eight
    coroutines, and a de-duplication bug could hide behind lucky scheduling.
    """

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.calls = 0
        self.release = asyncio.Event()
        self._response = response

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await self.release.wait()
        if isinstance(self._response, Exception):
            raise self._response
        return httpx.Response(
            self._response.status_code,
            headers=self._response.headers,
            content=self._response.content,
        )


def _http(handler, *, burst: int = 1, deadline_s: float = 10.0) -> ScholarHttp:
    clock = FakeClock()
    return ScholarHttp(
        mailto="scholar-tests@aleph-fixture.org",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_wait_min=0.0,
        retry_wait_max=0.0,
        burst=burst,
        deadline_s=deadline_s,
        clock=clock,
        sleep=clock.sleep,
    )


async def _launch(coros: list) -> list:
    tasks = [asyncio.create_task(c) for c in coros]
    await asyncio.sleep(0.05)  # let every task reach its first await
    return tasks


async def test_identical_concurrent_queries_hit_upstream_once() -> None:
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    # burst=1: if de-duplication were broken, the other seven would also have
    # to queue behind the rate limiter, which is the timeout half of the bug.
    http = _http(upstream, burst=1)

    tasks = await _launch(
        [http.get(_URL, params={"search": "graph neural networks"}) for _ in range(_FANOUT)]
    )
    upstream.release.set()
    responses = await asyncio.gather(*tasks)

    assert upstream.calls == 1
    assert len(responses) == _FANOUT
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == {"results": []} for r in responses)


async def test_distinct_concurrent_queries_are_not_collapsed() -> None:
    """De-duplication keyed on the query, not on the host.

    The failure mode on this side is silent and much worse than a slow search:
    eight different questions all receiving the first one's answer.
    """
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    http = _http(upstream, burst=_FANOUT)

    tasks = await _launch(
        [http.get(_URL, params={"search": f"question {i}"}) for i in range(_FANOUT)]
    )
    upstream.release.set()
    await asyncio.gather(*tasks)

    assert upstream.calls == _FANOUT


async def test_same_url_different_params_are_distinct_flights() -> None:
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    http = _http(upstream, burst=4)

    tasks = await _launch(
        [
            http.get(_URL, params={"search": "x", "per-page": "10"}),
            http.get(_URL, params={"per-page": "10", "search": "x"}),  # same, reordered
            http.get(_URL, params={"search": "x", "per-page": "25"}),  # different
        ]
    )
    upstream.release.set()
    await asyncio.gather(*tasks)

    assert upstream.calls == 2


async def test_de_duplication_is_not_a_cache() -> None:
    """A finished flight is forgotten; the next identical query goes upstream."""
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    upstream.release.set()
    http = _http(upstream, burst=5)

    await http.get(_URL, params={"search": "same"})
    await http.get(_URL, params={"search": "same"})

    assert upstream.calls == 2


async def test_a_shared_flight_shares_its_failure() -> None:
    """Every waiter gets the leader's error — and the leader's attempts only."""
    upstream = GatedUpstream(httpx.ConnectError("refused", request=httpx.Request("GET", _URL)))
    # A 0.1s budget at the 0.05s backoff floor buys two or three attempts —
    # the exact count is float dust on the deadline comparison. What matters is
    # that it is the leader's sequence and not one per waiter.
    http = _http(upstream, burst=_FANOUT, deadline_s=0.1)

    tasks = await _launch([http.get(_URL, params={"search": "q"}) for _ in range(_FANOUT)])
    # Released only once all eight have joined: a leader that fails before the
    # followers register would start eight flights and the count would prove
    # nothing.
    upstream.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert 1 <= upstream.calls <= 3  # the leader's attempts, not 3 x 8
    assert upstream.calls < _FANOUT
    assert all(isinstance(r, ScholarUnavailable) for r in results)


async def test_a_follower_giving_up_does_not_cancel_the_flight() -> None:
    """One caller walking away must not fail the seven still waiting.

    Without `asyncio.shield`, cancelling a follower propagates into the shared
    task, so one abandoned request would turn into an outage for everyone who
    joined its flight.
    """
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    http = _http(upstream, burst=1)

    tasks = await _launch([http.get(_URL, params={"search": "q"}) for _ in range(3)])
    tasks[2].cancel()
    upstream.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert upstream.calls == 1
    assert results[0].status_code == 200
    assert results[1].status_code == 200
    assert isinstance(results[2], asyncio.CancelledError)


async def test_dedup_does_not_leak_the_flight_registry() -> None:
    """A completed flight is removed, success or failure.

    A leaked key would pin every future identical query to a finished
    response — a permanent stale answer, and the reason the removal is in a
    `finally` rather than after the await.
    """
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    upstream.release.set()
    http = _http(upstream, burst=5)
    await http.get(_URL, params={"search": "ok"})

    failing = GatedUpstream(httpx.ConnectError("refused", request=httpx.Request("GET", _URL)))
    failing.release.set()
    http_failing = _http(failing, burst=5, deadline_s=0.1)
    with pytest.raises(ScholarUnavailable):
        await http_failing.get(_URL, params={"search": "bad"})

    assert http._inflight == {}
    assert http_failing._inflight == {}


async def test_cancelling_the_leader_does_not_kill_the_flight() -> None:
    """One disconnecting client must not fail everybody else's search.

    The leader used to `await task` unshielded, and awaiting a task propagates
    the awaiter's cancellation INTO it. FastAPI cancels a request task when the
    client goes away, so a browser closing a tab killed the flight and every
    other search that had joined it — a failure mode the pre-de-duplication code
    could not have had. An optimisation that introduces a new way to break other
    people's requests is not an optimisation.

    The companion test cancels a FOLLOWER, which the shield on the follower path
    already covered. This is the half that was missing.
    """
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    http = _http(upstream, burst=1)

    tasks = await _launch([http.get(_URL, params={"search": "same"}) for _ in range(3)])
    tasks[0].cancel()  # the leader's caller disconnected
    upstream.release.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError), "the leader should see its own cancel"
    for index, result in enumerate(results[1:], start=1):
        assert not isinstance(result, BaseException), (
            f"follower {index} was killed by the leader disconnecting: {result!r}"
        )
        assert result.status_code == 200
    assert upstream.calls == 1


async def test_the_flight_is_forgotten_when_it_finishes_not_when_a_waiter_leaves() -> None:
    """A cancelled leader's cleanup must not clear the in-flight entry while the
    request is still running — doing so lets the next caller start the second
    request de-duplication exists to avoid."""
    upstream = GatedUpstream(httpx.Response(200, json={"results": []}))
    http = _http(upstream, burst=1)

    leader = (await _launch([http.get(_URL, params={"search": "same"})]))[0]
    leader.cancel()
    await asyncio.sleep(0.05)

    # A newcomer arriving while the abandoned flight is still in progress joins
    # it rather than starting a second one.
    late = (await _launch([http.get(_URL, params={"search": "same"})]))[0]
    upstream.release.set()
    response = await late
    with contextlib.suppress(asyncio.CancelledError):
        await leader

    assert response.status_code == 200
    assert upstream.calls == 1, f"the abandoned flight was restarted: {upstream.calls} calls"
