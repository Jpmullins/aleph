"""An undeliverable contact address is a degradation, and it says so (`WS-E2`).

Aleph ships `ALEPH_SCHOLAR_MAILTO=dev@aleph.local`. It is well formed and cannot
receive mail, and the polite pool is granted on a contactable address — so
`ScholarHttp` sent a `mailto=` that bought nothing, clamped its own rate to the
POLITE ceiling it had not been granted, and reported none of that anywhere. The
three tests below pin the places that now do.

**This is an entitlement gap, not the cause of the 429s that opened `WS-E2`.**
The audit said the placeholder mailto was why eight concurrent searches returned
0/8. Measured 2026-08-22, same URL, same User-Agent, same `mailto=`, same host,
differing only in IP address family:

    curl -4 …/works?search=long+context&mailto=dev@aleph.local -> 429, Retry-After 25668
    curl -6 …/works?search=long+context&mailto=dev@aleph.local -> 200

Dropping the mailto and the User-Agent changed nothing over IPv4. Crossref over
the same IPv4 answered 200. OpenAlex is blocking this deployment's IPv4 egress
for about 7.1 hours; the host escapes it by preferring IPv6 and the API
container, being IPv4-only, does not. Nothing in this file fixes that, and
nothing in this file claims to.
"""

from __future__ import annotations

import httpx
import pytest
from scholar_test_support import FakeClock

from aleph_scholar.errors import ScholarUnavailable
from aleph_scholar.http import (
    COMMON_POOL_CEILING_PER_SECOND,
    POLITE_POOL_CEILING_PER_SECOND,
    ScholarHttp,
    is_contactable,
)
from aleph_scholar.openalex import OpenAlexClient

#: The address the deployment actually ran with. Named literally: the check has
#: to reject THIS, not merely reject something.
SHIPPED_PLACEHOLDER = "dev@aleph.local"

REAL = "research@aleph-fixture.org"


def _http(handler, *, mailto: str, rate_per_second: float | None = None) -> ScholarHttp:
    clock = FakeClock()
    return ScholarHttp(
        mailto=mailto,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_wait_min=0.0,
        retry_wait_max=0.0,
        rate_per_second=rate_per_second,
        clock=clock,
        sleep=clock.sleep,
    )


@pytest.mark.parametrize(
    "address",
    [
        SHIPPED_PLACEHOLDER,
        "ops@aleph.localhost",
        "ops@aleph.invalid",
        "ops@aleph.test",
        "ops@aleph.example",
        "ops@aleph.internal",
        "ops@example.com",
        "ops@example.org",
        "",
        "not-an-address",
        "ops@",
        "@aleph-fixture.org",
        "ops@nodots",
    ],
)
def test_a_placeholder_address_is_not_contactable(address: str) -> None:
    assert is_contactable(address) is False, f"{address!r} would be treated as a real contact"


@pytest.mark.parametrize(
    "address",
    ["research@aleph-fixture.org", "Ops@Aleph-Fixture.ORG", " ops@a.co ", "a+b@sub.domain.ac.uk"],
)
def test_a_deliverable_address_is_contactable(address: str) -> None:
    assert is_contactable(address) is True


async def test_a_placeholder_mailto_is_not_claimed_in_the_user_agent() -> None:
    """`(mailto:dev@aleph.local)` asserts a contact that does not exist.

    The software still identifies itself; it just stops making the promise the
    polite pool is a promise about.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={})

    await _http(handler, mailto=SHIPPED_PLACEHOLDER).get("https://api.crossref.org/works/10.1/x")
    assert seen == ["aleph-scholar/0.1"]

    seen.clear()
    await _http(handler, mailto=REAL).get("https://api.crossref.org/works/10.1/x")
    assert seen == [f"aleph-scholar/0.1 (mailto:{REAL})"]


async def test_openalex_stops_sending_a_mailto_it_cannot_honour() -> None:
    """Driven through the real `OpenAlexClient`, not through `mailto_params`.

    The producer/consumer pair is what matters: `mailto_params` returning `{}`
    is worth nothing if the client keeps interpolating `self._http.mailto`, and
    that is exactly what it did.
    """
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"results": []})

    await OpenAlexClient(_http(handler, mailto=SHIPPED_PLACEHOLDER)).search("x")
    assert "mailto" not in seen[-1].params, f"still claiming a contact: {seen[-1]}"

    await OpenAlexClient(_http(handler, mailto=REAL)).search("x")
    assert seen[-1].params["mailto"] == REAL


async def test_a_placeholder_mailto_is_clamped_to_the_common_pool() -> None:
    """No contactable address means no polite pool, so no polite-pool rate.

    The substantive half of the degradation. Clamping a configured 5/s to the
    POLITE ceiling clamps it to a budget the deployment was never granted.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    placeholder = _http(handler, mailto=SHIPPED_PLACEHOLDER, rate_per_second=5.0)
    assert placeholder.polite is False
    assert placeholder.rate_per_second == COMMON_POOL_CEILING_PER_SECOND

    real = _http(handler, mailto=REAL, rate_per_second=5.0)
    assert real.polite is True
    assert real.rate_per_second == 5.0
    assert COMMON_POOL_CEILING_PER_SECOND < POLITE_POOL_CEILING_PER_SECOND


async def test_a_throttled_request_names_the_mailto_as_the_cause() -> None:
    """The 429 stops being silent.

    `str(exc)` is what `routes/scholar.py::_upstream_response` logs
    (`error=str(exc)[:1000]`), so this sentence is what an operator reads in the
    API log next to the 503 instead of "the upstream is unavailable".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(handler, mailto=SHIPPED_PLACEHOLDER).get("https://api.openalex.org/works")
    message = str(caught.value)
    assert "HTTP 429" in message
    assert "ALEPH_SCHOLAR_MAILTO" in message, message
    assert SHIPPED_PLACEHOLDER in message, message

    # And a deployment that IS configured must not be told to configure itself.
    with pytest.raises(ScholarUnavailable) as caught_polite:
        await _http(handler, mailto=REAL).get("https://api.openalex.org/works")
    assert "ALEPH_SCHOLAR_MAILTO" not in str(caught_polite.value)


async def test_the_note_is_not_attached_to_failures_it_does_not_explain() -> None:
    """A 500 says nothing about pool membership.

    Attaching the mailto note to every upstream failure is how a real signal
    becomes noise an operator learns to scroll past.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(ScholarUnavailable) as caught:
        await _http(handler, mailto=SHIPPED_PLACEHOLDER).get("https://api.openalex.org/works")
    assert "ALEPH_SCHOLAR_MAILTO" not in str(caught.value)
