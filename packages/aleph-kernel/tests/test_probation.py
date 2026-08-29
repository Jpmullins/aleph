"""Probation and rollback (D5).

The activation gate catches a plugin broken at load. This catches one that
breaks later — which is the failure that matters for anything an agent authored,
because a fault that only appears on the second call is exactly the fault a
one-shot admission check cannot see.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.context import Context
from aleph_kernel.kernel import Kernel, State
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok, problem

pytestmark = pytest.mark.asyncio

Inv = Callable[[], Awaitable[None]]


def degrades_after(calls: int, *, name: str = "flaky", log: list[str] | None = None):
    """A capability whose probe passes `calls` times and then starts failing."""
    state = {"n": 0}

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide(name, object())
        if log is not None:

            async def down() -> None:
                log.append(f"down:{name}")

            yield down

    async def probe(ctx: Context) -> ProbeResult:
        state["n"] += 1
        if state["n"] <= calls:
            return ok(f"call {state['n']}")
        return problem(f"degraded on call {state['n']}")

    return CapabilitySpec(name=name, setup=setup, probe=probe, provides=frozenset({name}))


def protected_core(name: str = "ledger"):
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide(name, object())
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return ok()

    return CapabilitySpec(name=name, setup=setup, probe=probe, provides=frozenset({name}))


async def test_a_capability_that_degrades_is_retired() -> None:
    """THE property. Passing once must not mean assumed-working forever."""
    log: list[str] = []
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    await api.install(degrades_after(1, log=log))
    assert "flaky" in kernel.active()

    report = await api.check_health()

    assert "retired" in report["flaky"], report
    assert "flaky" not in kernel.active()
    assert kernel.state_of("flaky") is State.FAILED
    assert log == ["down:flaky"], "the retired capability leaked its resource"


async def test_retirement_withdraws_the_binding() -> None:
    """A capability nobody can rely on must not still be resolvable."""
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    await api.install(degrades_after(1))
    assert kernel.is_provided("flaky")

    await api.check_health()
    assert not kernel.is_provided("flaky")


async def test_a_healthy_capability_survives_the_check() -> None:
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    await api.install(degrades_after(99, name="solid"))

    report = await api.check_health()
    assert report["solid"] == "ok"
    assert "solid" in kernel.active()


async def test_the_health_check_reports_every_plugin() -> None:
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    await api.install(degrades_after(99, name="good"))
    await api.install(degrades_after(1, name="bad"))

    report = await api.check_health()
    assert set(report) == {"good", "bad"}


async def test_core_capability_is_not_reprobed_by_the_agent() -> None:
    """A probe is a live read. An agent must not be able to loop them at core."""
    reads = {"n": 0}

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide("ledger", object())
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        reads["n"] += 1
        return ok()

    kernel = Kernel()
    kernel.register_core(
        CapabilitySpec(
            name="ledger",
            setup=setup,
            probe=probe,
            provides=frozenset({"ledger"}),
        )
    )
    await kernel.boot()
    at_boot = reads["n"]

    api = AgentPluginAPI(kernel)
    await api.check_health()
    await api.check_health()

    assert reads["n"] == at_boot, "the agent triggered live reads against core"


async def test_reprobing_something_inactive_is_not_an_error() -> None:
    kernel = Kernel()
    kernel.register_core(protected_core("db"))
    result = await kernel.reprobe("db")
    assert not result.passed
    assert "not active" in result.detail


async def test_a_probe_that_raises_retires_the_capability() -> None:
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide("x", object())
        if False:  # pragma: no cover
            yield

    calls = {"n": 0}

    async def probe(ctx: Context) -> ProbeResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return ok()
        msg = "connection reset"
        raise ConnectionError(msg)

    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    await api.install(CapabilitySpec(name="x", setup=setup, probe=probe, provides=frozenset({"x"})))
    report = await api.check_health()
    assert "connection reset" in report["x"]
    assert "x" not in kernel.active()


# ---------------------------------------------------------------------------
# A probe that never returns
#
# A probe is a liveness check against something external, so "it never came
# back" is a normal outcome and it has to be a REPORTED one. Observed in
# practice: a port-forwarder had taken Redis's port on the host, so `ping()`
# accepted the TCP connection and never replied — `boot()` hung forever and the
# process reported nothing at all. No failure, no log line, no exit. Forty
# minutes of silence is worse than a red light.
# ---------------------------------------------------------------------------


async def test_a_probe_that_never_answers_is_a_failure_not_a_hang() -> None:
    import asyncio

    from aleph_kernel.errors import ProbeFailed

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide("wedged:key", "value")
        if False:  # pragma: no cover
            yield

    async def probe(_ctx: Context) -> ProbeResult:
        await asyncio.sleep(3600)  # a service that accepts and never answers
        return ok("unreachable")  # pragma: no cover

    kernel = Kernel(probe_timeout_s=0.05)
    kernel.register_dynamic(
        CapabilitySpec(name="wedged", setup=setup, probe=probe, provides=frozenset({"wedged:key"}))
    )

    with pytest.raises(ProbeFailed, match="did not answer"):
        await asyncio.wait_for(kernel.boot(), timeout=5)


async def test_a_timed_out_probe_unwinds_like_any_other_failure() -> None:
    """A hang must not leak what setup built — the capability is absent, not
    half-present."""
    import asyncio

    from aleph_kernel.errors import ProbeFailed

    closed: list[str] = []

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide("wedged:key", "value")

        async def close() -> None:
            closed.append("closed")

        yield close

    async def probe(_ctx: Context) -> ProbeResult:
        await asyncio.sleep(3600)
        return ok("unreachable")  # pragma: no cover

    kernel = Kernel(probe_timeout_s=0.05)
    kernel.register_dynamic(
        CapabilitySpec(name="wedged", setup=setup, probe=probe, provides=frozenset({"wedged:key"}))
    )
    with pytest.raises(ProbeFailed):
        await kernel.boot()

    assert closed == ["closed"], "a timed-out probe leaked what setup had built"
    assert not kernel.is_provided("wedged:key")


async def test_a_probe_inside_the_deadline_is_untouched() -> None:
    """The deadline must not be a latency budget nobody asked for: a probe that
    does real work — a round trip to Postgres, a write and read through the
    asset store — has to keep passing."""
    import asyncio

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide("slow:key", "value")
        if False:  # pragma: no cover
            yield

    async def probe(_ctx: Context) -> ProbeResult:
        await asyncio.sleep(0.01)
        return ok("answered, slowly")

    kernel = Kernel(probe_timeout_s=1.0)
    kernel.register_dynamic(
        CapabilitySpec(name="slow", setup=setup, probe=probe, provides=frozenset({"slow:key"}))
    )
    await kernel.boot()
    assert kernel.is_provided("slow:key")
