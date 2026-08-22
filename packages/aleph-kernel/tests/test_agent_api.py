"""The agent's plugin surface, and the guardrail that makes it safe.

The product thesis: an agent that creates plugins for itself and activates or
deactivates them as needed, with guardrails preventing it removing load-bearing
capability.

The guardrail under test is ADDRESSABILITY, not policy. Core capability has no
id an agent can construct, so removing it is unexpressible rather than refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID, uuid4

import pytest

from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.context import Context
from aleph_kernel.kernel import Kernel, PluginId
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok, problem

pytestmark = pytest.mark.asyncio

Inv = Callable[[], Awaitable[None]]


def working(name: str, *, provides: tuple[str, ...] = (), requires: tuple[str, ...] = ()):
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        for key in provides:
            ctx.provide(key, f"{name}:{key}")
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return ok(f"{name} responded")

    return CapabilitySpec(
        name=name,
        setup=setup,
        probe=probe,
        provides=frozenset(provides),
        requires=frozenset(requires),
    )


def broken(name: str, *, log: list[str] | None = None):
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        if log is not None:
            log.append("opened")

            async def close() -> None:
                log.append("closed")

            yield close

    async def probe(ctx: Context) -> ProbeResult:
        return problem("it cannot answer a read")

    return CapabilitySpec(name=name, setup=setup, probe=probe)


def core(name: str, *, provides: tuple[str, ...] = (), requires: tuple[str, ...] = ()):
    spec = working(name, provides=provides, requires=requires)
    return CapabilitySpec(
        name=spec.name,
        setup=spec.setup,
        probe=spec.probe,
        provides=spec.provides,
        requires=spec.requires,
        protected=True,
    )


async def booted() -> tuple[Kernel, AgentPluginAPI]:
    k = Kernel()
    k.register_core(core("ledger", provides=("ledger",)))
    k.register_core(core("db", provides=("db",)))
    await k.boot()
    return k, AgentPluginAPI(k)


# -- install ------------------------------------------------------------------


async def test_an_agent_can_install_a_working_capability() -> None:
    _, api = await booted()
    outcome = await api.install(working("summariser", provides=("summarise",)))
    assert outcome.installed, outcome.detail
    assert outcome.plugin_id


async def test_a_capability_that_cannot_prove_itself_is_refused() -> None:
    """The probe gate is the admission test for anything an agent authors."""
    log: list[str] = []
    _, api = await booted()
    outcome = await api.install(broken("hopeful", log=log))
    assert not outcome.installed
    assert "cannot answer a read" in outcome.detail
    assert log == ["opened", "closed"], "a refused capability leaked its resource"


async def test_a_refusal_explains_itself() -> None:
    _, api = await booted()
    outcome = await api.install(broken("hopeful"))
    assert outcome.detail.startswith("refused:")


async def test_a_plugin_cannot_claim_protection() -> None:
    _, api = await booted()
    spec = working("sneaky")
    outcome = await api.install(
        CapabilitySpec(name=spec.name, setup=spec.setup, probe=spec.probe, protected=True)
    )
    assert not outcome.installed
    assert "never from a plugin" in outcome.detail


# -- the guardrail ------------------------------------------------------------


async def test_core_capability_has_no_id_the_agent_can_see() -> None:
    """THE guardrail. Not refused — unnameable."""
    _, api = await booted()
    views = {v.name: v for v in api.inspect()}
    assert views["ledger"].protected
    assert views["ledger"].plugin_id is None
    assert views["ledger"].removable is False


async def test_disabling_something_the_agent_cannot_name_fails_informatively() -> None:
    """Even a forged id must not reach core, and must explain why."""
    _, api = await booted()
    outcome = await api.disable(PluginId(uuid4()))
    assert not outcome.installed
    assert "no id" in outcome.detail


async def test_an_agent_can_remove_what_it_installed() -> None:
    k, api = await booted()
    outcome = await api.install(working("scratch"))
    removed = await api.disable(PluginId(__import__("uuid").UUID(outcome.plugin_id)))
    assert "disabled" in removed.detail
    assert "scratch" not in k.active()


async def test_removal_is_refused_when_something_depends_on_it() -> None:
    _, api = await booted()
    provider = await api.install(working("indexer", provides=("index",)))
    await api.install(working("reporter", requires=("index",)))

    outcome = await api.disable(PluginId(__import__("uuid").UUID(provider.plugin_id)))
    assert "refused" in outcome.detail
    assert "reporter" in outcome.detail


async def test_a_refused_removal_changes_nothing() -> None:
    k, api = await booted()
    provider = await api.install(working("indexer", provides=("index",)))
    await api.install(working("reporter", requires=("index",)))
    before = k.active()

    await api.disable(PluginId(__import__("uuid").UUID(provider.plugin_id)))
    assert k.active() == before


async def test_force_breaks_the_agents_own_plugins_but_not_core() -> None:
    k = Kernel()
    k.register_core(core("ledger", provides=("ledger",)))
    api = AgentPluginAPI(k)
    provider = await api.install(working("indexer", provides=("index",)))
    await api.install(working("reporter", requires=("index",)))
    await k.boot()

    outcome = await api.disable(PluginId(__import__("uuid").UUID(provider.plugin_id)), force=True)
    assert "disabled" in outcome.detail
    assert "reporter" in outcome.detail
    assert "ledger" in k.active(), "force reached protected capability"


# -- inspect ------------------------------------------------------------------


async def test_inspect_shows_the_blast_radius_before_acting() -> None:
    """An agent that cannot predict a refusal will keep triggering it."""
    _, api = await booted()
    await api.install(working("indexer", provides=("index",)))
    await api.install(working("reporter", requires=("index",)))

    views = {v.name: v for v in api.inspect()}
    assert views["indexer"].would_also_stop == ("reporter",)
    assert views["indexer"].removable is False
    assert views["reporter"].removable is True


async def test_inspect_does_not_change_anything() -> None:
    k, api = await booted()
    before = k.active()
    api.inspect()
    api.inspect()
    assert k.active() == before


# ---------------------------------------------------------------------------
# The two bugs that break the FIRST agent-authored plugin.
#
# Neither was covered by any of the 142 tests that came before, and they are
# not edge cases: they are literally the first two things that happen when an
# agent starts writing plugins for itself. The first attempt is wrong, and the
# second attempt is version two of the same name.
# ---------------------------------------------------------------------------


async def test_a_failed_install_leaves_no_ghost() -> None:
    """A refused install must leave the graph exactly as it found it.

    `boot` and `activate` both re-derive the topological order over the WHOLE
    registered set. A refused spec left behind therefore breaks every later
    install too, for the life of the process — the agent's first mistake would
    be permanent and would present as an unrelated failure.
    """
    kernel = Kernel()
    api = AgentPluginAPI(kernel)

    refused = await api.install(working("needs-a-ghost", requires=("nobody-provides-this",)))
    assert refused.installed is False

    # An unrelated, valid plugin must still install.
    good = await api.install(working("unrelated"))
    assert good.installed is True, f"the ghost poisoned the graph: {good.detail}"

    # And the whole graph must still boot.
    await kernel.boot()


async def test_a_failed_install_does_not_break_boot() -> None:
    """The same property from the other end: `boot` must not raise afterwards."""
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    await api.install(working("dangling", requires=("nothing-provides-this",)))
    await kernel.boot()
    assert kernel.active() == frozenset()


async def test_an_agent_can_ship_a_second_version() -> None:
    """Install, disable, install the same name again.

    Before `unregister`, the second install returned "'mine' is already
    registered" — so an agent could improve a plugin exactly zero times without
    a process restart.
    """
    kernel = Kernel()
    api = AgentPluginAPI(kernel)

    first = await api.install(working("mine", provides=("mine:v1",)))
    assert first.installed is True
    assert first.plugin_id is not None

    stopped = await api.disable(PluginId(UUID(first.plugin_id)), unregister=True)
    assert stopped.installed is False

    second = await api.install(working("mine", provides=("mine:v2",)))
    assert second.installed is True, second.detail
    assert kernel.is_provided("mine:v2")


async def test_disable_returns_the_id_it_was_given() -> None:
    """Disabling must not cost the agent the handle to its own plugin.

    `_teardown` leaves `plugin_id` intact and `replace` works fine on a disabled
    plugin, so the handle existed all along — the agent was simply never given
    it back, and `replace` was unreachable after a disable.
    """
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    installed = await api.install(working("iterating", provides=("iterating:v1",)))
    assert installed.plugin_id is not None
    pid = PluginId(UUID(installed.plugin_id))

    outcome = await api.disable(pid)
    assert outcome.plugin_id == str(pid), "the handle was thrown away"

    # And the handle still works.
    new_id = await kernel.replace(pid, working("iterating", provides=("iterating:v2",)))
    assert new_id != pid
    assert kernel.is_provided("iterating:v2")


async def test_disable_without_unregister_keeps_the_registration() -> None:
    """ "Stopped" and "gone" are different states, and the agent chooses."""
    from uuid import UUID

    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    installed = await api.install(working("paused"))
    pid = PluginId(UUID(str(installed.plugin_id)))

    await api.disable(pid)
    assert "paused" in kernel._mounted

    await api.disable(pid, unregister=True)
    assert "paused" not in kernel._mounted


async def test_unregister_refuses_a_protected_capability() -> None:
    """`unregister` is addressed by NAME, which sidesteps the id guardrail.

    The whole point of `PluginId` is that core capability has none, so removal
    is unexpressible rather than refused. A name-addressed method has no such
    property, so the `protected` check inside it is the only defence — and it
    must be the first statement in the method.
    """
    from aleph_kernel.errors import ProtectedCapability

    kernel = Kernel()
    spec = working("the-ledger")
    kernel.register_core(CapabilitySpec(**{**spec.__dict__, "protected": True}))

    with pytest.raises(ProtectedCapability):
        kernel.unregister("the-ledger")
    assert "the-ledger" in kernel._mounted, "it was dropped before the check ran"


async def test_unregister_refuses_an_active_capability() -> None:
    """An active capability holds live effects and may have dependents.

    Dropping the registration would leave its effects unwound and anything that
    requires it holding a binding nothing provides. It has to go through
    `deactivate`, which computes the blast radius first.
    """
    kernel = Kernel()
    kernel.register_dynamic(working("live", provides=("live:key",)))
    await kernel.boot()

    with pytest.raises(ValueError, match="active"):
        kernel.unregister("live")
    assert kernel.is_provided("live:key")


async def test_unregister_of_an_unknown_name_is_a_keyerror() -> None:
    kernel = Kernel()
    with pytest.raises(KeyError):
        kernel.unregister("never-existed")


# ---------------------------------------------------------------------------
# The ghost, on the branch an agent actually hits
# ---------------------------------------------------------------------------


async def test_an_install_the_PROBE_refused_leaves_no_ghost() -> None:
    """`test_a_failed_install_leaves_no_ghost` never reaches this branch.

    It installs a spec with an unsatisfiable `requires`, which raises
    `MissingProvider` out of `topological_order` — the generic `except
    Exception` arm. The `except ProbeFailed` arm has its own
    `_unregister_quietly` call and nothing exercised it: deleting that line
    left all 153 kernel tests green, including both tests written for exactly
    this defect.

    It is the likelier arm by a distance. An agent's first plugin usually
    resolves its dependencies fine and then cannot prove it works — that is
    what a probe gate is FOR. So the fix as tested covered the rarer half of
    the failure it was written for, and the half that poisons the process for
    the rest of its life was open.
    """
    kernel = Kernel()
    api = AgentPluginAPI(kernel)

    refused = await api.install(broken("first-attempt"))
    assert refused.installed is False
    assert "refused" in refused.detail, refused.detail

    # The name is free again — an agent's second attempt is version two of the
    # same plugin, not a new one with a different name.
    second = await api.install(working("first-attempt", provides=("first-attempt",)))
    assert second.installed is True, f"the refused install held its name: {second.detail}"

    # An unrelated plugin still installs, and the whole graph still boots.
    # Both re-derive the topological order over everything registered, which is
    # why one leftover would break them all.
    other = await api.install(working("unrelated"))
    assert other.installed is True, f"the ghost poisoned the graph: {other.detail}"
    await kernel.boot()


async def test_unregistering_a_plugin_also_unregisters_what_came_down_with_it() -> None:
    """`disable(force=True, unregister=True)` must not leave half a ghost.

    The dependents were torn down as collateral, so they are droppable too —
    and if they are left REGISTERED, the flag has moved the ghost one level out
    rather than removed it: the next install of a dependent's name fails with
    "already registered", which is precisely the message `unregister` exists to
    stop an agent from ever seeing.

    Nothing covered this. `test_an_agent_can_ship_a_second_version` uses
    `unregister=True` on a plugin with no dependents, so the loop over
    `radius.collateral` is a loop over nothing and deleting it changed no test.
    """
    kernel = Kernel()
    api = AgentPluginAPI(kernel)

    base = await api.install(working("store", provides=("store",)))
    assert base.plugin_id is not None
    dependent = await api.install(working("indexer", provides=("index",), requires=("store",)))
    assert dependent.installed is True, dependent.detail

    stopped = await api.disable(PluginId(UUID(base.plugin_id)), force=True, unregister=True)
    assert stopped.installed is False
    assert "indexer" in stopped.detail, stopped.detail

    # Both names are free. Reinstalling the DEPENDENT is the half that was open:
    # it is the one `unregister` had to loop over to reach.
    again = await api.install(working("indexer", provides=("index",)))
    assert again.installed is True, (
        f"the collateral kept its registration: {again.detail}. `unregister` "
        "dropped the target and left the plugins it took down as ghosts."
    )
    # And the target's own name, with a real assertion: `install(...) is not
    # None` would be satisfied by every refusal an InstallOutcome can carry.
    target_again = await api.install(working("store", provides=("store",)))
    assert target_again.installed is True, target_again.detail
    assert target_again.plugin_id is not None
