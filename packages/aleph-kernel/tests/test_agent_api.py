"""The agent's plugin surface, and the guardrail that makes it safe.

The product thesis: an agent that creates plugins for itself and activates or
deactivates them as needed, with guardrails preventing it removing load-bearing
capability.

The guardrail under test is ADDRESSABILITY, not policy. Core capability has no
id an agent can construct, so removing it is unexpressible rather than refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import uuid4

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
