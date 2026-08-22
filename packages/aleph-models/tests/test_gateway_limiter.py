"""The ceiling has to hold, and it has to actually be the thing holding.

Two failures are equally bad and look identical from a green test:

* **The limiter does not bound anything.** A `Semaphore` that is constructed and
  never acquired passes every grep. That is why the plan's original criterion
  (``grep -rn 'Semaphore' capabilities.py`` returns >= 1) was dropped: it cannot
  fail in any interesting way.
* **The limiter serialises everything to one.** A ceiling of 4 that admits one
  request at a time also never exceeds 4. The gateway would be politely idle
  while an agent turn ran six times slower than it should, and no assertion of
  the form ``peak <= ceiling`` would notice.

So every concurrency test here asserts BOTH bounds: ``peak <= ceiling`` and
``peak == ceiling``.

The load is deliberately the real mixture — 20 `LiteLLMClient.chat()` calls and
one `autoconfigure` probe sweep — because they are separate call paths that must
share one door. Nothing in the chat path is handed a limiter, and nothing in
`autoconfigure_bindings` knows what a limiter is; both reach the same endpoint,
so both must be metered by the same object.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from aleph_core.errors import GatewayUnavailable
from aleph_core.schemas.model_profile import Capability
from aleph_models.autoconfigure import autoconfigure_bindings
from aleph_models.client import ChatMessage, LiteLLMClient
from aleph_models.discovery import GatewayCatalog
from aleph_models.limiter import (
    GatewayLimiter,
    LimitedTransport,
    LimiterConfig,
    close_gateway_clients,
    configure_limits,
    endpoint_key,
    limiter_for,
    reset_limiters,
    shared_gateway_client,
)
from aleph_models.pricing import PricingTable
from aleph_models.testing import FakeGateway, GatewayConfig, RecordingSessions
from aleph_security.principal import Principal

CHAT = "/v1/chat/completions"


@pytest.fixture(autouse=True)
def _clean_registry() -> AsyncIterator[None]:
    """The registry is process-global; a test that configures it must hand it back.

    Deliberately autouse. A leaked ceiling from one test is invisible in that
    test and changes the result of the next one, which is the worst shape a test
    failure can take.
    """
    reset_limiters()
    yield
    reset_limiters()


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(), subject="test", email="t@example.com", actor_kind="aleph_agent"
    )


def _client(
    fake: FakeGateway, http: httpx.AsyncClient, sessions: RecordingSessions
) -> LiteLLMClient:
    """A real client with NO limiter argument — the point of the test.

    If this had to be handed a limiter, the criterion would be measuring the
    test's wiring rather than the system's.
    """
    return LiteLLMClient(
        base_url=fake.base_url,
        api_key=fake.api_key,
        http_client=http,
        pricing=PricingTable(),
        session_maker=cast("Any", sessions),
    )


async def _chat(client: LiteLLMClient) -> None:
    await client.chat(
        principal=_principal(),
        project_id=uuid4(),
        agent_run_id=None,
        capability=Capability.CLASSIFICATION,
        profile_bindings={"classification": {"model": "claude-haiku-4-5"}},
        messages=[ChatMessage(role="user", content="hello")],
        purpose="test.limiter",
    )


async def _probe_sweep(fake: FakeGateway, http: httpx.AsyncClient) -> None:
    """The real `autoconfigure` fan-out: every advertised model, probed at once."""
    profile = SimpleNamespace(bindings_jsonb={})
    await autoconfigure_bindings(
        cast("Any", profile),
        catalog=GatewayCatalog(base_url=fake.base_url, api_key=fake.api_key, client=http),
        base_url=fake.base_url,
        api_key=fake.api_key,
        http_client=http,
        probe=True,
    )


class TestTheCeilingHolds:
    @pytest.mark.parametrize("ceiling", [1, 4])
    async def test_mixed_fan_out_never_exceeds_and_always_reaches_the_ceiling(
        self, ceiling: int
    ) -> None:
        configure_limits(LimiterConfig(max_concurrency=ceiling, queue_timeout_s=10.0))
        fake = FakeGateway(GatewayConfig.well_behaved(latency_s=0.05))
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = _client(fake, http, sessions)
            await asyncio.gather(
                *(_chat(client) for _ in range(20)),
                _probe_sweep(fake, http),
            )

        assert fake.peak_in_flight <= ceiling, (
            f"{fake.peak_in_flight} requests were in flight at once against a gateway "
            f"limited to {ceiling}"
        )
        assert fake.peak_in_flight == ceiling, (
            "the ceiling was never reached: the limiter is serialising rather than "
            "saturating, which bounds the gateway by making Aleph slow"
        )
        assert fake.count(CHAT) >= 20, "the chats did not actually reach the gateway"
        assert len(sessions.model_calls()) == 20, "a limited call must still be costed"

    async def test_the_limiter_and_the_gateway_agree_on_the_peak(self) -> None:
        """The limiter's own counter is the number `/readyz` would report.

        Kept honest against the fake's independently-recorded peak: a stat that
        is written by the thing it describes and read by nobody else is how a
        dashboard ends up confidently wrong.
        """
        configure_limits(LimiterConfig(max_concurrency=3, queue_timeout_s=10.0))
        fake = FakeGateway(GatewayConfig.well_behaved(latency_s=0.05))
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = _client(fake, http, sessions)
            await asyncio.gather(*(_chat(client) for _ in range(12)))
        limiter = limiter_for(fake.base_url)
        assert limiter.stats.peak_in_flight == fake.peak_in_flight == 3
        assert limiter.stats.admitted == 12
        assert limiter.stats.in_flight == 0, "a released slot was not given back"


class TestTheDoorIsPerEndpoint:
    def test_the_two_spellings_of_one_gateway_share_a_door(self) -> None:
        """`LiteLLMClient` is built with the bare URL and `ChatOpenAI` with `/v1`.

        Two doors for one server is the same as none: the agent's six subagents
        would fan out through their own ceiling while the retrieval path used
        another, and the sum is what the gateway sees.
        """
        bare = limiter_for("https://gw.example.com")
        with_v1 = limiter_for("https://gw.example.com/v1")
        trailing = limiter_for("https://gw.example.com/")
        assert bare is with_v1 is trailing

    def test_two_gateways_are_two_doors(self) -> None:
        assert limiter_for("https://a.example.com") is not limiter_for("https://b.example.com")

    def test_the_key_is_the_server_not_the_string(self) -> None:
        assert endpoint_key("HTTPS://GW.example.com/v1/") == "https://gw.example.com"


class TestRefusingIsBetterThanHanging:
    async def test_a_request_that_cannot_be_admitted_is_refused_with_a_reason(self) -> None:
        limiter = GatewayLimiter(LimiterConfig(max_concurrency=1, queue_timeout_s=0.05))
        async with limiter.slot():
            with pytest.raises(GatewayUnavailable) as raised:
                await limiter.acquire()
        assert "concurrency ceiling" in str(raised.value)
        assert limiter.stats.refused == 1

    async def test_a_refusal_does_not_lower_the_ceiling_for_ever(self) -> None:
        """The leak that looks exactly like a gateway getting slower."""
        limiter = GatewayLimiter(LimiterConfig(max_concurrency=1, queue_timeout_s=0.05))
        async with limiter.slot():
            with pytest.raises(GatewayUnavailable):
                await limiter.acquire()
        async with limiter.slot():
            assert limiter.stats.in_flight == 1


class TestTheRateLimit:
    async def test_a_configured_rpm_spaces_requests_out(self) -> None:
        """No wall clock: the bucket's clock and sleep are injected."""
        now = [0.0]
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        limiter = GatewayLimiter(
            LimiterConfig(max_concurrency=8, rpm=60, burst=1, queue_timeout_s=30.0),
            clock=lambda: now[0],
            sleep=_sleep,
        )
        async with limiter.slot():
            pass
        async with limiter.slot():
            pass
        assert slept == [pytest.approx(1.0)], f"60/min should space requests 1s apart, got {slept}"

    async def test_rpm_is_off_unless_configured(self) -> None:
        """Aleph cannot discover somebody's quota, and inventing one throttles a
        local endpoint that has none."""
        assert LimiterConfig().rpm == 0
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:  # pragma: no cover - must not run
            slept.append(seconds)

        limiter = GatewayLimiter(LimiterConfig(max_concurrency=2), sleep=_sleep)
        for _ in range(5):
            async with limiter.slot():
                pass
        assert slept == []


class TestTheTransportSeam:
    async def test_the_slot_is_held_until_the_body_is_finished(self) -> None:
        """The half that makes the limiter mean anything for the agent.

        `ChatOpenAI` streams. Releasing when `handle_async_request` returns would
        count a thirty-second streaming turn as an instant, so the ceiling would
        bound header exchanges and nothing else.
        """
        fake = FakeGateway(GatewayConfig.well_behaved())
        limiter = GatewayLimiter(LimiterConfig(max_concurrency=2))
        transport = LimitedTransport(limiter, inner=fake.transport())
        async with httpx.AsyncClient(transport=transport, base_url=fake.base_url) as http:
            async with http.stream(
                "POST",
                CHAT,
                headers={"Authorization": f"Bearer {fake.api_key}"},
                json={"model": "claude-haiku-4-5", "messages": []},
            ) as response:
                assert limiter.stats.in_flight == 1, (
                    "the slot was released as soon as the headers arrived; a streamed "
                    "response would not be counted at all"
                )
                await response.aread()
            assert limiter.stats.in_flight == 0, "the slot was never given back"

    async def test_a_transport_failure_gives_the_slot_back(self) -> None:
        class _Broken(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("nope", request=request)

        limiter = GatewayLimiter(LimiterConfig(max_concurrency=1, queue_timeout_s=0.05))
        async with httpx.AsyncClient(
            transport=LimitedTransport(limiter, inner=_Broken()), base_url="http://x.invalid"
        ) as http:
            with pytest.raises(httpx.ConnectError):
                await http.get("/")
        assert limiter.stats.in_flight == 0


class TestHealthIsNotBehindTheDoor:
    async def test_a_saturated_gateway_still_answers_the_healthcheck(self) -> None:
        """Deliberate, and pinned so it is not "tidied up" into the door.

        `/readyz` calls `LiteLLMClient.health()`, compose wires `/readyz` as the
        API's healthcheck, and Docker restarts a container that fails it.
        Queueing the health probe behind eight in-flight agent calls would
        report the gateway as down whenever it is merely busy — and restarting
        Aleph does nothing about somebody else's endpoint while taking the whole
        stack down with it.
        """
        configure_limits(LimiterConfig(max_concurrency=1, queue_timeout_s=0.05))
        fake = FakeGateway(GatewayConfig.well_behaved())
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = _client(fake, http, sessions)
            async with limiter_for(fake.base_url).slot():
                assert await client.health() is True


class TestTheAgentSeam:
    """`shared_gateway_client` is the one line `copilot_agent` needs.

    `ChatOpenAI` builds its own HTTP client, so this is the only place a limiter
    can sit under the agent without forking langchain. The call site itself
    lives in `apps/api/src/aleph_api/copilot_agent.py`, which this workstream
    may not edit — so the seam is pinned here rather than shipped untested,
    because an exported helper nothing exercises is a contract with no caller.
    """

    async def test_one_client_per_endpoint_through_that_endpoints_door(self) -> None:
        configure_limits(LimiterConfig(max_concurrency=2, queue_timeout_s=5.0))
        first = shared_gateway_client("https://gw.example.com/v1", timeout=30.0)
        second = shared_gateway_client("https://gw.example.com", timeout=30.0)
        assert first is second, (
            "seven ChatOpenAI instances would otherwise hold seven connection pools"
        )
        transport = first._transport  # the private wiring IS the assertion
        assert isinstance(transport, LimitedTransport)
        assert transport._limiter is limiter_for("https://gw.example.com")
        await close_gateway_clients()
        assert first.is_closed

    async def test_the_agents_traffic_is_bounded_by_the_same_ceiling(self) -> None:
        """The ceiling holds on the client `shared_gateway_client` BUILT.

        This test used to replace `client._transport` with a `LimitedTransport`
        of its own before making any request — so it exercised its own wiring
        and would have passed against a helper that returned a plain
        `httpx.AsyncClient` with no limiter at all. Proven: an adversarial
        reviewer made `shared_gateway_client` return exactly that, and this test
        stayed green.

        Now only the INNER transport is swapped, which is the part that has to
        be a fake because there is no real gateway here. The limiter stays
        whatever the helper put there, so a helper that forgets to install one
        fails on `peak_in_flight`.
        """
        configure_limits(LimiterConfig(max_concurrency=2, queue_timeout_s=5.0))
        fake = FakeGateway(GatewayConfig.well_behaved(latency_s=0.05))
        client = shared_gateway_client(fake.base_url, timeout=5.0)
        outer = client._transport
        assert isinstance(outer, LimitedTransport), (
            "shared_gateway_client returned a client with no limiter — the whole "
            "point of the helper"
        )
        outer._inner = fake.transport()
        try:
            await asyncio.gather(
                *(
                    client.get(
                        f"{fake.base_url}/v1/models",
                        headers={"Authorization": f"Bearer {fake.api_key}"},
                    )
                    for _ in range(8)
                )
            )
        finally:
            await close_gateway_clients()
        assert fake.peak_in_flight == 2
