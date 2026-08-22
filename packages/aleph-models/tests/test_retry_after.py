"""A 429 that says when to come back must be believed.

The old policy waited 1s, 2s, 4s and read no headers at all, so three attempts
against a gateway asking for seven seconds were three requests that could not
succeed — sent while the endpoint was already over its budget, which is the one
moment adding traffic is worst. `aleph_scholar.http` has honoured `Retry-After`
against far friendlier upstreams since it was written.

The wait is asserted through the real policy with an injected `sleep`, not with
a stopwatch. A seven second assertion measured in wall-clock is a test nobody
writes twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage, LiteLLMClient
from aleph_models.limiter import reset_limiters
from aleph_models.pricing import PricingTable
from aleph_models.retry import MAX_RETRY_AFTER_S, gateway_retry, retry_after_seconds
from aleph_models.testing import FakeGateway, GatewayConfig, RecordingSessions, rate_limited
from aleph_security.principal import Principal

CHAT = "/v1/chat/completions"


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    reset_limiters()
    yield
    reset_limiters()


def _response(status: int, **headers: str) -> httpx.Response:
    request = httpx.Request("POST", "http://gw.invalid/v1/chat/completions")
    return httpx.Response(status, headers=headers, request=request)


async def _waits_for(response: httpx.Response) -> list[float]:
    """Drive the real policy over a failing call and record what it slept."""
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    attempts = 0
    with pytest.raises(httpx.HTTPStatusError):
        async for attempt in gateway_retry(sleep=_sleep):
            with attempt:
                attempts += 1
                raise httpx.HTTPStatusError(
                    "rate limited", request=response.request, response=response
                )
    assert attempts == 3, "the policy is still three attempts"
    return slept


class TestParsingTheHeader:
    def test_delta_seconds(self) -> None:
        assert retry_after_seconds(_response(429, **{"Retry-After": "7"})) == 7.0

    def test_absent_means_nothing_was_said(self) -> None:
        assert retry_after_seconds(_response(429)) is None

    def test_http_date_is_measured_against_the_servers_own_clock(self) -> None:
        """A client clock a minute fast turns 'wait 7s' into 'wait none'."""
        served = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        response = _response(
            429,
            **{
                "Retry-After": format_datetime(served + timedelta(seconds=7)),
                "Date": format_datetime(served),
            },
        )
        assert retry_after_seconds(response) == pytest.approx(7.0)

    def test_a_date_in_the_past_is_zero_not_negative(self) -> None:
        served = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        response = _response(
            429,
            **{
                "Retry-After": format_datetime(served - timedelta(seconds=30)),
                "Date": format_datetime(served),
            },
        )
        assert retry_after_seconds(response) == 0.0

    def test_junk_falls_back_rather_than_raising(self) -> None:
        assert retry_after_seconds(_response(429, **{"Retry-After": "soon"})) is None


class TestThePolicyHonoursIt:
    async def test_retry_after_7_waits_seven_seconds(self) -> None:
        assert await _waits_for(_response(429, **{"Retry-After": "7"})) == [7.0, 7.0]

    async def test_no_header_keeps_the_exponential_schedule(self) -> None:
        """The fallback is unchanged, so the header path cannot be proved by a
        test that would pass either way."""
        assert await _waits_for(_response(429)) == [1.0, 2.0]

    async def test_a_5xx_has_no_header_to_read(self) -> None:
        assert await _waits_for(_response(503)) == [1.0, 2.0]

    async def test_an_absurd_retry_after_is_capped(self) -> None:
        """`Retry-After: 3600` reports an exhausted key, not a queue position.
        Sleeping it out inside one request is a hang with no error message."""
        waits = await _waits_for(_response(429, **{"Retry-After": "3600"}))
        assert waits == [MAX_RETRY_AFTER_S, MAX_RETRY_AFTER_S]


class TestTheClientUsesThePolicy:
    async def test_a_rate_limited_chat_waits_what_the_gateway_asked_for(self) -> None:
        """End to end: the header the gateway sent decides the wait the client takes."""
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)

        fake = FakeGateway(
            GatewayConfig.well_behaved(invoke_script=(rate_limited(retry_after="7"),))
        )
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = LiteLLMClient(
                base_url=fake.base_url,
                api_key=fake.api_key,
                http_client=http,
                pricing=PricingTable(),
                session_maker=cast("Any", sessions),
                retry_sleep=_sleep,
            )
            response = await client.chat(
                principal=Principal(
                    user_id=uuid4(),
                    subject="test",
                    email="t@example.com",
                    actor_kind="aleph_agent",
                ),
                project_id=uuid4(),
                agent_run_id=None,
                capability=Capability.CLASSIFICATION,
                profile_bindings={"classification": {"model": "claude-haiku-4-5"}},
                messages=[ChatMessage(role="user", content="hi")],
                purpose="test.retry_after",
            )
        assert slept == [7.0], f"the gateway asked for 7 seconds and the client waited {slept}"
        assert fake.count(CHAT) == 2, "the 429 was not retried"
        assert response.choices[0].message.content == "pong"

    async def test_a_retry_takes_a_slot_of_its_own(self) -> None:
        """A retry is another request arriving at the same endpoint.

        Counting only the first attempt under-reports in-flight traffic exactly
        when the gateway is already over budget — the moment the number matters.
        """
        from aleph_models.limiter import GatewayLimiter, LimiterConfig

        async def _sleep(_seconds: float) -> None:
            return None

        limiter = GatewayLimiter(LimiterConfig(max_concurrency=2))
        fake = FakeGateway(GatewayConfig.well_behaved(invoke_script=(rate_limited(),)))
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = LiteLLMClient(
                base_url=fake.base_url,
                api_key=fake.api_key,
                http_client=http,
                pricing=PricingTable(),
                session_maker=cast("Any", sessions),
                limiter=limiter,
                retry_sleep=_sleep,
            )
            await client.chat(
                principal=Principal(
                    user_id=uuid4(),
                    subject="test",
                    email="t@example.com",
                    actor_kind="aleph_agent",
                ),
                project_id=uuid4(),
                agent_run_id=None,
                capability=Capability.CLASSIFICATION,
                profile_bindings={"classification": {"model": "claude-haiku-4-5"}},
                messages=[ChatMessage(role="user", content="hi")],
                purpose="test.retry_slot",
            )
        assert limiter.stats.admitted == 2, "the retried attempt did not go through the door"
