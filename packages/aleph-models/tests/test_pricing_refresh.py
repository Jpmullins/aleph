"""A gateway that comes up after boot gets to price the calls it serves.

Discovery ran once, in `models()` setup, and `refresh_pricing` swallows an
unreachable gateway so the process still boots. Together those two correct
decisions produced one silent, permanent failure: a gateway that was not there
at that instant was never asked again, so every `ModelCall` for the life of the
process carried `pricing_source="unknown"` and `cost_usd=0` — a spend ledger
reading $0.00 across a live run, which is the exact shape of defect
`aleph_models.pricing`'s own docstring was written about.

The headline test below is the one that matters, and it is deliberately not a
unit test of the refresher. It builds a **real** `LiteLLMClient` against a table
that is empty because the gateway is genuinely refusing connections, makes a
**real** chat call through it, and asserts the recorded row is unpriced. Then
the gateway comes up, the refresher ticks, and the SAME client — never rebuilt,
never reconfigured — records a gateway-priced row for the next call. Asserting
`pricing.has(...)` instead would prove the table changed and say nothing about
whether the thing that bills anybody ever noticed.

No test here sleeps an interval. `sleep` and `clock` are constructor arguments
for that reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage, LiteLLMClient
from aleph_models.discovery import GatewayCatalog
from aleph_models.limiter import reset_limiters
from aleph_models.pricing import PricingTable, get_default_pricing
from aleph_models.repricing import (
    DEFAULT_REFRESH_S,
    DEFAULT_RETRY_S,
    PricingRefresher,
    refresh_intervals,
)
from aleph_models.testing import FakeGateway, FakeModel, GatewayConfig, RecordingSessions
from aleph_security.principal import Principal

MODEL = "claude-haiku-4-5"


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """The limiter registry is process-global; hand it back."""
    reset_limiters()
    yield
    reset_limiters()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FlakyTransport(httpx.AsyncBaseTransport):
    """Refuses every connection until `up`, then delegates to the fake gateway.

    A gateway that is *down* is not one answering 500 — it is a TCP connection
    nobody accepts. `httpx.ConnectError` is what that looks like from inside
    `discover_models`, and it is the branch `refresh_pricing` catches and
    swallows, which is what made the failure permanent and invisible.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.up = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            msg = "connection refused"
            raise httpx.ConnectError(msg, request=request)
        return await self._inner.handle_async_request(request)


class Clock:
    """A hand-driven clock and sleeper.

    `sleep` records what it was asked to wait, advances the notional time by
    exactly that much, and then blocks until the test releases it. So the
    refresher's own interval choice is observable (`slept`) without any of it
    being waited on.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.asleep = asyncio.Event()
        self._release: asyncio.Event | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.now += delay
        gate = asyncio.Event()
        self._release = gate
        self.asleep.set()
        await gate.wait()

    def wake(self) -> None:
        self.asleep.clear()
        gate, self._release = self._release, None
        if gate is not None:
            gate.set()


async def settle(predicate: Callable[[], bool], *, what: str, timeout_s: float = 5.0) -> None:
    """Poll the event loop until `predicate` holds. Bounded, and never sleeps long."""

    async def _poll() -> None:
        # A yield-to-the-loop poll, not an Event: what is being waited on is a
        # counter inside the subject, and instrumenting the subject with an
        # Event so its test could observe it would be the test changing what it
        # measures. Bounded by `wait_for` below.
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_poll(), timeout_s)
    except TimeoutError:
        raise AssertionError(f"timed out waiting for {what}") from None


async def tick(refresher: PricingRefresher, clock: Clock) -> None:
    """Let exactly one sweep run, and settle at the NEXT sleep.

    Ending at the next sleep rather than at the sweep is what makes `slept`
    deterministic: after N ticks it holds exactly N+1 entries — the N that were
    released, plus the one currently pending — so the interval the refresher
    chose *after* a result is directly observable.
    """
    await settle(clock.asleep.is_set, what="the refresher to reach its sleep")
    before = refresher.cycles
    clock.wake()
    await settle(
        lambda: refresher.cycles > before and clock.asleep.is_set(),
        what="one refresh sweep and the sleep that follows it",
    )


def a_client(
    fake: FakeGateway, http: httpx.AsyncClient, pricing: PricingTable, sessions: RecordingSessions
) -> LiteLLMClient:
    return LiteLLMClient(
        base_url=fake.base_url,
        api_key=fake.api_key,
        http_client=http,
        pricing=pricing,
        session_maker=cast("Any", sessions),
    )


async def a_chat(client: LiteLLMClient) -> None:
    await client.chat(
        principal=Principal(
            user_id=uuid4(), subject="t", email="t@example.com", actor_kind="aleph_agent"
        ),
        project_id=uuid4(),
        agent_run_id=None,
        capability=Capability.SYNTHESIS,
        profile_bindings={"synthesis": {"model": MODEL}},
        messages=[ChatMessage(role="user", content="hello")],
        purpose="test.repricing",
    )


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


async def test_a_gateway_that_comes_up_after_boot_prices_calls_with_no_restart() -> None:
    """The whole workstream criterion, driven through the real cost path.

    The sequence is the one a compose stack produces: the API wins the race and
    discovers nothing, the gateway finishes starting a moment later, and calls
    then flow perfectly well — priced at nothing, forever, because the one
    discovery this process was ever going to run has already been and gone.
    """
    fake = FakeGateway(GatewayConfig.well_behaved())
    flaky = FlakyTransport(fake.transport())
    clock = Clock()
    sessions = RecordingSessions()

    async with httpx.AsyncClient(transport=flaky, base_url=fake.base_url) as http:
        # --- boot, with the gateway still refusing connections --------------
        pricing = get_default_pricing()
        catalog = GatewayCatalog(base_url=fake.base_url, api_key=fake.api_key, client=http)
        assert await catalog.refresh_pricing(pricing) == 0
        assert pricing.models() == [], "the gateway was down; nothing should be priced"

        client = a_client(fake, http, pricing, sessions)
        refresher = PricingRefresher(
            catalog=catalog, pricing=pricing, sleep=clock.sleep, clock=clock
        )
        refresher.start()
        try:
            # --- the gateway finishes starting. Calls work; nothing re-asks --
            flaky.up = True
            await a_chat(client)
            first = sessions.model_calls()[-1]
            assert first.pricing_source == "unknown", (
                "this is the defect: a perfectly successful call, billed at nothing"
            )
            assert first.cost_usd == Decimal("0")

            # --- one sweep. Nothing is restarted or rebuilt ------------------
            await tick(refresher, clock)
            assert refresher.priced > 0, "the sweep found no rates"

            # --- the SAME client now prices the next call --------------------
            await a_chat(client)
            second = sessions.model_calls()[-1]
            assert second.pricing_source == "gateway", (
                "the client was never rebuilt, so this is the in-place merge "
                "reaching the table it was constructed with"
            )
            assert second.cost_usd > Decimal("0")
        finally:
            await refresher.stop()

    assert refresher.last_priced_at is not None
    assert not refresher.running


# ---------------------------------------------------------------------------
# The interval
# ---------------------------------------------------------------------------


async def test_the_retry_is_short_until_the_gateway_answers_and_slow_afterwards() -> None:
    """One interval would have to choose between a slow repair and a hot loop."""
    fake = FakeGateway(GatewayConfig.well_behaved())
    flaky = FlakyTransport(fake.transport())
    clock = Clock()

    async with httpx.AsyncClient(transport=flaky, base_url=fake.base_url) as http:
        pricing = get_default_pricing()
        catalog = GatewayCatalog(base_url=fake.base_url, api_key=fake.api_key, client=http)
        refresher = PricingRefresher(
            catalog=catalog,
            pricing=pricing,
            interval_s=900.0,
            retry_interval_s=30.0,
            sleep=clock.sleep,
            clock=clock,
        )
        refresher.start()
        try:
            await tick(refresher, clock)  # still down
            assert clock.slept == [30.0, 30.0]
            await tick(refresher, clock)  # still down
            assert clock.slept == [30.0, 30.0, 30.0]

            flaky.up = True
            await tick(refresher, clock)
            assert clock.slept == [30.0, 30.0, 30.0, 900.0], (
                "the long interval must start only once the gateway has answered"
            )
        finally:
            await refresher.stop()


async def test_a_reachable_gateway_that_reports_no_rates_is_not_hammered() -> None:
    """A restricted virtual key lists models and prices nothing. That is normal.

    Keying the switch on `pricing.models()` instead of on reachability would
    retry every thirty seconds forever against the deployment shape Aleph
    actually runs in.

    The model is named `vllm-local-…` on purpose: the hints file claims the
    real ids in `DEFAULT_MODELS`, so a gateway reporting no rate for
    `claude-haiku-4-5` still ends up with a priced table and this test would
    pass whatever the switch is keyed on.
    """
    fake = FakeGateway(
        # Hostile default kept — /model/info 403s — plus a model nothing prices.
        GatewayConfig(models=(FakeModel(id="vllm-local-qwen3-32b", mode="chat"),))
    )
    clock = Clock()

    async with fake.client() as http:
        pricing = PricingTable()
        catalog = GatewayCatalog(base_url=fake.base_url, api_key=fake.api_key, client=http)
        refresher = PricingRefresher(
            catalog=catalog,
            pricing=pricing,
            interval_s=900.0,
            retry_interval_s=30.0,
            sleep=clock.sleep,
            clock=clock,
        )
        refresher.start()
        try:
            await tick(refresher, clock)
            assert catalog.cached, "the fake listed its models via the /v1/models fallback"
            assert pricing.models() == [], "nothing priced it — that is the premise"
            assert clock.slept == [30.0, 900.0]
        finally:
            await refresher.stop()


# ---------------------------------------------------------------------------
# Failure and shutdown
# ---------------------------------------------------------------------------


class _Exploding:
    """A catalog whose refresh raises something `refresh_pricing` does not catch."""

    def __init__(self) -> None:
        self.calls = 0
        #: `PricingRefresher.next_interval` reads this to decide reachability.
        self.cached: list[Any] = []

    async def refresh_pricing(self, pricing: PricingTable, *, force: bool = False) -> int:
        self.calls += 1
        msg = "name resolution temporarily failed"
        raise OSError(msg)


async def test_every_sweep_actually_re_asks_the_gateway() -> None:
    """The catalog has its own five-minute TTL, and a sweep must bypass it.

    Without `force=True` a sweep inside that window returns the cached list and
    merges nothing — so a refresher on a thirty-second retry would tick nine
    times and re-ask once, and the nine ticks would all report success.
    """
    fake = FakeGateway(GatewayConfig.well_behaved())
    clock = Clock()

    async with fake.client() as http:
        catalog = GatewayCatalog(
            base_url=fake.base_url, api_key=fake.api_key, client=http, ttl_s=300.0
        )
        refresher = PricingRefresher(
            catalog=catalog, pricing=PricingTable(), sleep=clock.sleep, clock=clock
        )
        refresher.start()
        try:
            await tick(refresher, clock)
            first = fake.count("/model/info")
            assert first == 1
            await tick(refresher, clock)
            assert fake.count("/model/info") == 2, (
                "the second sweep was served from the catalog's TTL cache"
            )
        finally:
            await refresher.stop()


def test_a_zero_interval_cannot_become_a_hot_loop() -> None:
    """A typo in an operator's env file must not turn into a DoS on the gateway."""
    catalog = _Exploding()
    refresher = PricingRefresher(
        catalog=cast("Any", catalog),
        pricing=PricingTable(),
        interval_s=0.0,
        retry_interval_s=-5.0,
    )
    assert refresher.next_interval() == 1.0
    catalog.cached = [object()]
    assert refresher.next_interval() == 1.0


async def test_an_unexpected_error_is_recorded_and_the_loop_survives_it() -> None:
    """`refresh_pricing` catches httpx and ValueError. Everything else got here.

    A refresher that ended on the first `OSError` from a DNS resolver is
    indistinguishable from one that was never started — the same silence the
    whole workstream is about.
    """
    catalog = _Exploding()
    clock = Clock()
    refresher = PricingRefresher(
        catalog=cast("Any", catalog), pricing=PricingTable(), sleep=clock.sleep, clock=clock
    )
    refresher.start()
    try:
        await tick(refresher, clock)
        assert refresher.last_error is not None
        assert "OSError" in refresher.last_error
        await tick(refresher, clock)
        assert catalog.calls == 2, "the loop stopped after the first failure"
    finally:
        await refresher.stop()


async def test_stop_cancels_the_task_and_reports_it_stopped() -> None:
    clock = Clock()
    refresher = PricingRefresher(
        catalog=cast("Any", _Exploding()), pricing=PricingTable(), sleep=clock.sleep, clock=clock
    )
    refresher.start()
    assert refresher.running
    await refresher.stop()
    assert not refresher.running


async def test_stop_gives_up_rather_than_hanging_on_a_task_that_ignores_cancellation() -> None:
    """`models` is protected in both manifests, so this inverse runs on every exit.

    An unbounded await here turns one wedged discovery request into a process
    that never terminates — which on a container platform is a rolling deploy
    that stalls.
    """
    cancels = 0

    async def stubborn(_delay: float) -> None:
        # Swallows the FIRST cancellation and carries on, which is what a
        # `finally:` block doing async cleanup, or a shielded request, looks
        # like from the outside. The second one is honoured so this test can
        # hand the event loop back at the end.
        nonlocal cancels
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancels += 1
                if cancels > 1:
                    raise

    refresher = PricingRefresher(
        catalog=cast("Any", _Exploding()), pricing=PricingTable(), sleep=stubborn
    )
    refresher.start()
    task = next(t for t in asyncio.all_tasks() if t.get_name() == "gateway.pricing_refresh")
    await asyncio.sleep(0)

    started = time.monotonic()
    try:
        # The outer bound is the test's, not the subject's: without it an
        # unbounded `stop()` HANGS the suite, and a hang is a worse signal than
        # a failure — CI reports it as a timeout with no named test.
        await asyncio.wait_for(refresher.stop(timeout_s=0.05), timeout=2.0)
    except TimeoutError:
        pytest.fail("stop() never returned; it is waiting on a task that will not stop")
    elapsed = time.monotonic() - started

    assert cancels == 1, "stop() did not cancel the task at all"
    assert elapsed < 2.0, f"stop() waited {elapsed:.2f}s on a task that will not stop"
    assert not refresher.running

    task.cancel()  # hand the loop back
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class _SettingsWithFields:
    aleph_gateway_pricing_refresh_s = 42.0
    aleph_gateway_pricing_retry_s = 7.0


def test_intervals_come_from_settings_then_the_environment_then_the_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALEPH_GATEWAY_PRICING_REFRESH_S", raising=False)
    monkeypatch.delenv("ALEPH_GATEWAY_PRICING_RETRY_S", raising=False)
    assert refresh_intervals(None) == (DEFAULT_REFRESH_S, DEFAULT_RETRY_S)

    monkeypatch.setenv("ALEPH_GATEWAY_PRICING_REFRESH_S", "120")
    monkeypatch.setenv("ALEPH_GATEWAY_PRICING_RETRY_S", "5")
    assert refresh_intervals(None) == (120.0, 5.0)

    # A Settings object that carries the fields wins over the environment, so a
    # field landing later needs no change in `refresh_intervals`.
    assert refresh_intervals(_SettingsWithFields()) == (42.0, 7.0)


def test_an_unparseable_interval_falls_back_instead_of_taking_the_process_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALEPH_GATEWAY_PRICING_REFRESH_S", "every-so-often")
    monkeypatch.setenv("ALEPH_GATEWAY_PRICING_RETRY_S", "0")
    assert refresh_intervals(None) == (DEFAULT_REFRESH_S, DEFAULT_RETRY_S)
