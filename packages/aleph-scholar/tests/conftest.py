"""Shared fixtures for aleph-scholar unit tests — no network, no real redis."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from scholar_test_support import FakeRedis

from aleph_scholar.http import ScholarHttp


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def make_http() -> Callable[[Callable[[httpx.Request], httpx.Response]], ScholarHttp]:
    """Build a ScholarHttp over httpx.MockTransport with zero retry waits."""

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> ScholarHttp:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return ScholarHttp(
            mailto="test@aleph.local",
            client=client,
            retry_wait_min=0.0,
            retry_wait_max=0.0,
        )

    return _make
