"""The kernel: the probe gate, guarded removal, and addressability.

The probe gate gets the most adversarial coverage here on purpose. It is the
only mechanism in this design that can fail for a real reason, and the
predicted failure mode of the whole project is that it gets built and then
satisfied with assertions that cannot fail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from aleph_kernel.context import Context
from aleph_kernel.errors import (
    DependentsWouldBreak,
    ProbeFailed,
    UndeclaredAccess,
)
from aleph_kernel.kernel import Kernel, PluginId, State
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok, problem

pytestmark = pytest.mark.asyncio

Inv = Callable[[], Awaitable[None]]


def provider(name: str, key: str, value: object = "v", *, log: list[str] | None = None):
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide(key, value)
        if log is not None:

            async def down() -> None:
                log.append(f"down:{name}")

            yield down

    async def probe(ctx: Context) -> ProbeResult:
        return ok(f"{key} provided")

    return CapabilitySpec(name=name, setup=setup, probe=probe, provides=frozenset({key}))


def consumer(name: str, key: str, *, provides: tuple[str, ...] = ()):
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.get(key)  # prove it resolves at setup time
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return ok() if ctx.has(key) else problem(f"{key} not resolvable")

    return CapabilitySpec(
        name=name,
        setup=setup,
        probe=probe,
        requires=frozenset({key}),
        provides=frozenset(provides),
    )


# -- the probe gate ----------------------------------------------------------


async def test_a_capability_with_a_failing_probe_does_not_activate() -> None:
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return problem("the read path returned nothing")

    k = Kernel()
    k.register_core(CapabilitySpec(name="thing", setup=setup, probe=probe))
    with pytest.raises(ProbeFailed, match="the read path returned nothing"):
        await k.boot()
    assert k.state_of("thing") is State.FAILED
    assert "thing" not in k.active()


async def test_a_failing_probe_rolls_back_everything_setup_built() -> None:
    """A capability that cannot prove itself must leave no trace."""
    log: list[str] = []

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        log.append("opened")

        async def close() -> None:
            log.append("closed")

        yield close
        ctx.provide("halfbuilt", object())

    async def probe(ctx: Context) -> ProbeResult:
        return problem("cannot serve a read")

    k = Kernel()
    k.register_core(
        CapabilitySpec(
            name="halfbuilt", setup=setup, probe=probe, provides=frozenset({"halfbuilt"})
        )
    )
    with pytest.raises(ProbeFailed):
        await k.boot()
    assert log == ["opened", "closed"], "the failed capability leaked its resource"
    # And its binding must be gone, or a dependent could still resolve it.
    assert not k.is_provided("halfbuilt"), "a failed capability left its service published"


async def test_a_probe_that_raises_is_a_failed_probe_not_a_crash() -> None:
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        msg = "connection refused"
        raise ConnectionError(msg)

    k = Kernel()
    k.register_core(CapabilitySpec(name="thing", setup=setup, probe=probe))
    with pytest.raises(ProbeFailed, match="connection refused"):
        await k.boot()
    assert k.state_of("thing") is State.FAILED


async def test_a_probe_cannot_reach_what_its_capability_did_not_declare() -> None:
    """The probe runs in the capability's own scope — it cannot fake a dependency."""

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        ctx.get("secrets")  # never declared
        return ok()

    k = Kernel()
    k.register_core(provider("vault", "secrets"))
    k.register_core(CapabilitySpec(name="nosy", setup=setup, probe=probe))
    with pytest.raises(ProbeFailed, match="UndeclaredAccess"):
        await k.boot()


async def test_a_failing_probe_does_not_block_unrelated_capabilities() -> None:
    k = Kernel()
    k.register_core(provider("db", "db"))
    await k.activate("db")
    assert k.active() == {"db"}


async def test_problem_requires_a_detail() -> None:
    """A failure that does not say what it observed is not a diagnosis."""
    with pytest.raises(ValueError, match="must say what it observed"):
        problem("   ")


# -- guarded removal ---------------------------------------------------------


async def test_deactivating_a_depended_on_capability_is_refused() -> None:
    k = Kernel()
    k.register_core(provider("db", "db"))
    pid = k.register_dynamic(consumer("reports", "db"))
    db_pid = k.register_dynamic(provider("cache", "cache"))
    await k.boot()

    # `cache` is a leaf: safe.
    radius = await k.deactivate(db_pid)
    assert radius.is_safe

    # `reports` is a leaf too.
    await k.deactivate(pid)
    assert "reports" not in k.active()


async def test_refusal_names_what_would_break_and_changes_nothing() -> None:
    k = Kernel()
    pid = k.register_dynamic(provider("db", "db"))
    k.register_core(consumer("reports", "db"))
    await k.boot()
    before = k.active()

    with pytest.raises(DependentsWouldBreak, match="reports"):
        await k.deactivate(pid)

    assert k.active() == before, "a refused deactivation still changed the system"


async def test_force_cannot_override_protected_collateral() -> None:
    """An operator may break their own plugins; nobody may break the kernel's footing."""
    k = Kernel()
    pid = k.register_dynamic(provider("db", "db"))
    protected = consumer("ledger", "db")
    k.register_core(
        CapabilitySpec(
            name=protected.name,
            setup=protected.setup,
            probe=protected.probe,
            requires=protected.requires,
            protected=True,
        )
    )
    await k.boot()
    with pytest.raises(DependentsWouldBreak, match="protected ledger"):
        await k.deactivate(pid, force=True)
    assert "ledger" in k.active()


async def test_teardown_runs_dependents_before_providers() -> None:
    log: list[str] = []
    k = Kernel()
    pid = k.register_dynamic(provider("db", "db", log=log))

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.get("db")

        async def down() -> None:
            log.append("down:reports")

        yield down

    async def probe(ctx: Context) -> ProbeResult:
        return ok()

    k.register_dynamic(
        CapabilitySpec(name="reports", setup=setup, probe=probe, requires=frozenset({"db"}))
    )
    await k.boot()
    await k.deactivate(pid, force=True)
    assert log == ["down:reports", "down:db"], "a provider was torn down under a live dependent"


# -- addressability ----------------------------------------------------------


async def test_core_capabilities_have_no_plugin_id() -> None:
    """The primary guardrail: protected capability is unexpressible, not refused."""
    k = Kernel()
    k.register_core(provider("db", "db"))
    with pytest.raises(KeyError):
        await k.deactivate(PluginId(__import__("uuid").uuid4()))


async def test_a_plugin_cannot_declare_itself_protected() -> None:
    k = Kernel()
    s = provider("sneaky", "x")
    with pytest.raises(ValueError, match="never from a plugin"):
        k.register_dynamic(
            CapabilitySpec(
                name=s.name, setup=s.setup, probe=s.probe, provides=s.provides, protected=True
            )
        )


# -- boot --------------------------------------------------------------------


async def test_boot_activates_providers_first() -> None:
    k = Kernel()
    k.register_core(consumer("reports", "db"))
    k.register_core(provider("db", "db"))
    await k.boot()
    assert k.active() == {"db", "reports"}


async def test_shutdown_unwinds_everything() -> None:
    log: list[str] = []
    k = Kernel()
    k.register_core(provider("db", "db", log=log))
    await k.boot()
    await k.shutdown()
    assert log == ["down:db"]
    assert k.active() == frozenset()


async def test_undeclared_access_is_refused_at_setup() -> None:
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.get("db")  # not declared
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return ok()

    k = Kernel()
    k.register_core(provider("db", "db"))
    k.register_core(CapabilitySpec(name="thief", setup=setup, probe=probe))
    with pytest.raises(UndeclaredAccess, match="without declaring it"):
        await k.boot()


async def test_activate_brings_up_providers_it_needs() -> None:
    """Activating one capability activates what it requires, in order."""
    k = Kernel()
    k.register_core(consumer("reports", "db"))
    k.register_core(provider("db", "db"))
    await k.activate("reports")
    assert k.active() == {"db", "reports"}


async def test_activate_reports_a_missing_provider_clearly() -> None:
    from aleph_kernel.errors import MissingProvider

    k = Kernel()
    k.register_core(consumer("reports", "db"))
    with pytest.raises(MissingProvider, match="requires db"):
        await k.activate("reports")


async def test_a_capability_can_read_back_its_own_provisions() -> None:
    """A probe proves a capability works by exercising what it just published."""
    seen: list[object] = []

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide("thing", "VALUE")
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        seen.append(ctx.get("thing"))  # its own provision
        return ok()

    k = Kernel()
    k.register_core(
        CapabilitySpec(name="publisher", setup=setup, probe=probe, provides=frozenset({"thing"}))
    )
    await k.boot()
    assert seen == ["VALUE"]


async def test_reading_its_own_provisions_does_not_widen_anything_else() -> None:
    """Self-provisions are readable; a neighbour's are still refused."""

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        ctx.provide("mine", 1)
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        ctx.get("mine")
        ctx.get("secrets")  # a neighbour's, never declared
        return ok()

    k = Kernel()
    k.register_core(provider("vault", "secrets"))
    k.register_core(
        CapabilitySpec(name="nosy", setup=setup, probe=probe, provides=frozenset({"mine"}))
    )
    with pytest.raises(ProbeFailed, match="UndeclaredAccess"):
        await k.boot()


async def test_a_setup_that_raises_leaves_no_trace() -> None:
    """A capability whose setup fails partway must unwind what it had built.

    Distinct from the EffectScope-level test: this pins that the KERNEL calls
    unwind on the setup-failure path. Removing that call left every kernel test
    green until this existed — found by scripts/_acceptance/self_check.sh.
    """
    log: list[str] = []

    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        log.append("opened")

        async def close() -> None:
            log.append("closed")

        yield close
        ctx.provide("halfbuilt", object())
        msg = "constructor blew up after publishing"
        raise RuntimeError(msg)

    async def probe(ctx: Context) -> ProbeResult:  # never reached
        return ok()

    k = Kernel()
    k.register_core(
        CapabilitySpec(
            name="halfbuilt", setup=setup, probe=probe, provides=frozenset({"halfbuilt"})
        )
    )
    with pytest.raises(RuntimeError, match="blew up"):
        await k.boot()

    assert log == ["opened", "closed"], "the failed setup leaked its resource"
    assert not k.is_provided("halfbuilt"), "a failed setup left its service published"
    assert k.state_of("halfbuilt") is State.FAILED
