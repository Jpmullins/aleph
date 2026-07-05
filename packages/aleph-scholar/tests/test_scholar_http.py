"""ScholarHttp: user agent, retry policy, Retry-After, token bucket."""

from __future__ import annotations

import httpx
import pytest

from aleph_scholar.errors import ScholarUpstreamError
from aleph_scholar.http import ScholarHttp


def _http(
    handler,
    *,
    sleeps: list[float] | None = None,
    clock=None,
    burst: int = 5,
):
    async def recording_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return ScholarHttp(
        mailto="test@aleph.local",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_wait_min=0.0,
        retry_wait_max=0.0,
        burst=burst,
        sleep=recording_sleep,
        **({"clock": clock} if clock is not None else {}),
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
        await _http(handler).get("https://api.openalex.org/works")


async def test_transport_error_raises_scholar_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ScholarUpstreamError):
        await _http(handler).get("https://api.openalex.org/works")


async def test_404_is_returned_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Resource not found.")

    response = await _http(handler).get("https://api.crossref.org/works/10.1/fake")
    assert response.status_code == 404


async def test_retry_after_is_honored_and_capped() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "9999"})
        return httpx.Response(200, json={})

    response = await _http(handler, sleeps=sleeps).get("https://api.openalex.org/works")
    assert response.status_code == 200
    assert 7.0 in sleeps
    assert 8.0 in sleeps  # 9999 capped at _RETRY_AFTER_CAP_S (8s)
    assert 9999.0 not in sleeps


async def test_token_bucket_throttles_after_burst() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    http = _http(handler, sleeps=sleeps, clock=lambda: 1000.0, burst=5)
    for _ in range(5):
        await http.get("https://api.openalex.org/works")
    assert sleeps == []  # burst allows 5 without waiting
    await http.get("https://api.openalex.org/works")
    assert sleeps == [1.0]  # 6th request waits for one token at 1 req/s


async def test_token_bucket_is_per_host() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    http = _http(handler, sleeps=sleeps, clock=lambda: 1000.0, burst=1)
    await http.get("https://api.openalex.org/works")
    await http.get("https://api.crossref.org/works")  # different host, fresh bucket
    assert sleeps == []
