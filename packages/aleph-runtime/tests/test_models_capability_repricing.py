"""The `models` capability starts the refresher, and unwinding stops it.

`PricingRefresher` is tested on its own in
`packages/aleph-models/tests/test_pricing_refresh.py`. This is the other half,
and the half this repository keeps getting wrong: a correct component with no
caller. `GatewayCatalog.refresh_pricing` was itself in exactly that state —
correct, tested, and invoked from precisely one line of setup that ran once.

So this drives the real `models()` capability generator, against a gateway that
refuses connections, and asserts on the object it publishes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from aleph_kernel import Context, EffectScope, Store
from aleph_runtime.capabilities import (
    DB_SESSIONS,
    GATEWAY_LIMITER,
    HTTP_GATEWAY,
    PRICING,
    PRICING_REFRESHER,
    REDIS,
    SETTINGS,
    models,
)


def _refusing_client() -> httpx.AsyncClient:
    """Every request is a refused connection — the gateway is not up yet."""

    def _refuse(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(_refuse))


def _settings() -> Any:
    return SimpleNamespace(
        litellm_base_url="http://gateway.invalid",
        insights_litellm_api_key="sk-test-not-a-real-key",
        aleph_gateway_pricing_refresh_s=900.0,
        aleph_gateway_pricing_retry_s=30.0,
    )


async def _mount(http: httpx.AsyncClient) -> tuple[Context, EffectScope]:
    """Run `models().setup(...)` the way the kernel runs it, and keep the scope."""
    from aleph_models.limiter import limiter_for

    spec = models()
    store = Store()
    scope = EffectScope("models")
    ctx = Context(
        owner="models",
        requires=spec.requires | spec.provides,
        # `provides` too: `Context.provide` refuses a key the owner did not
        # declare, so a test double that omits it cannot run the real setup.
        # The kernel passes this from the spec; a double must do the same or it
        # is exercising a different object.
        provides=spec.provides,
        optional=spec.optional,
        store=store,
        scope=scope,
    )
    store.put("root", SETTINGS, _settings(), "test")
    store.put("root", HTTP_GATEWAY, http, "test")
    store.put("root", DB_SESSIONS, lambda: None, "test")
    store.put("root", REDIS, None, "test")
    store.put("root", GATEWAY_LIMITER, limiter_for("http://gateway.invalid"), "test")
    await scope.drive(spec.setup(ctx))
    return ctx, scope


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    from aleph_models.limiter import reset_limiters

    reset_limiters()
    yield
    reset_limiters()


async def test_mounting_models_leaves_a_refresher_running() -> None:
    """Boot with the gateway down. The table is empty AND something is retrying.

    Empty-and-retrying and empty-and-abandoned are the same table. The whole
    defect was that only the second one existed.
    """
    async with _refusing_client() as http:
        ctx, scope = await _mount(http)
        try:
            pricing = ctx.get(PRICING)
            refresher = ctx.get(PRICING_REFRESHER)
            assert pricing.models() == [], "the gateway refused every connection"
            assert refresher.running, (
                "nothing will ever ask the gateway again; every call this "
                "process serves will record pricing_source=unknown"
            )
            assert refresher.next_interval() == 30.0, (
                "an unreachable gateway should be on the short retry"
            )
        finally:
            await scope.unwind()


async def test_unwinding_stops_the_refresher() -> None:
    """`models` is protected in both manifests: this inverse runs on every exit."""
    async with _refusing_client() as http:
        ctx, scope = await _mount(http)
        refresher = ctx.get(PRICING_REFRESHER)
        assert refresher.running
        await scope.unwind()
        assert not refresher.running, "the refresh task outlived the capability that owns it"


async def test_the_probe_line_says_whether_an_unpriced_model_is_being_retried() -> None:
    """A probe reporting "3 unpriced" leaves an operator with nothing to do.

    Whether the gap is self-healing decides whether the answer is "wait" or
    "go and look at the gateway", so the sentence the `models` probe appends
    has to say which. This asserts on `describe()`, which is the string the
    probe interpolates — the probe itself needs a seeded database and is
    covered by the acceptance run.
    """
    async with _refusing_client() as http:
        ctx, scope = await _mount(http)
        try:
            refresher = ctx.get(PRICING_REFRESHER)
            assert refresher.describe() == "retrying every 30s"
            await refresher.stop()
            assert refresher.describe() == "NOT retrying — the pricing refresher is not running"
        finally:
            await scope.unwind()
