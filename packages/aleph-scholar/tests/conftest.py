"""Shared fixtures for aleph-scholar unit tests — no network, no real redis."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from scholar_test_support import FakeClock, FakeRedis

from aleph_scholar.http import ScholarHttp


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def make_http() -> Callable[[Callable[[httpx.Request], httpx.Response]], ScholarHttp]:
    """Build a ScholarHttp over httpx.MockTransport with a simulated clock.

    The clock is simulated rather than real because the retry budget is now a
    deadline in seconds. With a real clock and a no-op sleep, a handler that
    keeps returning 500 would spin for the whole deadline — thousands of
    pointless iterations per test. Advancing `FakeClock` from the sleep makes
    the same test terminate in a handful of iterations, deterministically.
    """

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> ScholarHttp:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clock = FakeClock()
        return ScholarHttp(
            mailto="scholar-tests@aleph-fixture.org",
            client=client,
            retry_wait_min=0.0,
            retry_wait_max=0.0,
            clock=clock,
            sleep=clock.sleep,
        )

    return _make
