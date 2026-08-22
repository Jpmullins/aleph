"""Which upstream failures are the request's fault, and which are an outage.

The defect this file pins: `ensure_ok` used to raise one exception type for
every status >= 400, and the API mapped that single type to 503 "the upstream
service is unavailable". So a 400 caused by a filter Aleph itself built wrong
was reported as the internet being down — the least actionable possible
answer to the most fixable possible cause.

`ScholarClientError` and `ScholarUnavailable` both still subclass
`ScholarUpstreamError`, and that is load-bearing: `verify_dois` and the
reviewer's doi_verification node fold *any* upstream trouble to `ok=None`, and
must never read a rejection as "this DOI does not exist".
"""

from __future__ import annotations

import httpx
import pytest
from scholar_test_support import FakeClock

from aleph_scholar.errors import (
    ScholarClientError,
    ScholarUnavailable,
    ScholarUpstreamError,
)
from aleph_scholar.http import ScholarHttp, ensure_ok

#: Every 4xx that is not 429 is the request's fault; 429 and 5xx are not.
CLIENT_FAULT_STATUSES = (400, 401, 403, 404, 409, 422)
UNAVAILABLE_STATUSES = (429, 500, 502, 503, 504)


def _http(handler, *, deadline_s: float = 1.0) -> ScholarHttp:
    clock = FakeClock()
    return ScholarHttp(
        mailto="scholar-tests@aleph-fixture.org",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_wait_min=0.0,
        retry_wait_max=0.0,
        deadline_s=deadline_s,
        clock=clock,
        sleep=clock.sleep,
    )


def _response(status: int, **kwargs) -> httpx.Response:
    request = httpx.Request("GET", "https://api.openalex.org/works?filter=doi:10.1%2Cx")
    return httpx.Response(status, request=request, **kwargs)


# --------------------------------------------------------------- ensure_ok


@pytest.mark.parametrize("status", CLIENT_FAULT_STATUSES)
def test_client_fault_statuses_are_not_outages(status: int) -> None:
    with pytest.raises(ScholarClientError) as caught:
        ensure_ok(_response(status, json={"error": "Invalid query parameters"}))
    exc = caught.value
    assert exc.status_code == status
    assert not isinstance(exc, ScholarUnavailable)  # the whole point
    assert isinstance(exc, ScholarUpstreamError)  # tri-state consumers still fold it


@pytest.mark.parametrize("status", UNAVAILABLE_STATUSES)
def test_retryable_statuses_reaching_ensure_ok_are_unavailable(status: int) -> None:
    with pytest.raises(ScholarUnavailable) as caught:
        ensure_ok(_response(status, headers={"Retry-After": "12"}))
    assert caught.value.status_code == status
    assert caught.value.retry_after == 12.0


def test_upstream_reason_is_carried_not_discarded() -> None:
    """The reason is the difference between actionable and mysterious."""
    with pytest.raises(ScholarClientError) as caught:
        ensure_ok(
            _response(
                400,
                json={
                    "error": "Invalid query parameters",
                    "message": "filter is not a valid field",
                },
            )
        )
    assert "Invalid query parameters" in caught.value.reason
    assert "filter is not a valid field" in caught.value.reason


def test_plain_text_reason_is_carried_too() -> None:
    """Crossref answers with a bare line, not JSON."""
    with pytest.raises(ScholarClientError) as caught:
        ensure_ok(_response(400, text="Invalid rows value"))
    assert caught.value.reason == "Invalid rows value"


def test_reason_is_truncated_so_an_html_error_page_cannot_become_the_body() -> None:
    with pytest.raises(ScholarClientError) as caught:
        ensure_ok(_response(400, text="x" * 5000))
    assert len(caught.value.reason) == 300


def test_2xx_passes_through() -> None:
    ok = _response(200, json={})
    assert ensure_ok(ok) is ok


# ------------------------------------------------------- through ScholarHttp


async def test_a_client_fault_is_never_retried() -> None:
    """Sending the same rejected request again produces the same rejection.

    Retrying it burns the caller's whole deadline and the rate-limit budget to
    arrive at the answer the first attempt already had.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "Invalid query parameters"})

    response = await _http(handler, deadline_s=10.0).get("https://api.openalex.org/works")
    assert response.status_code == 400  # get() hands it back; ensure_ok classifies it
    assert len(calls) == 1


async def test_a_persistent_429_is_unavailable_and_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "5"})

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(handler, deadline_s=4.0).get("https://api.openalex.org/works")
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 5.0


async def test_a_transport_failure_has_no_upstream_status() -> None:
    """No response means no status to report — and nothing to blame the caller for."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(handler).get("https://api.openalex.org/works")
    assert caught.value.status_code is None
    assert caught.value.retry_after is None
