"""Pins replace `protected`, and the guardrail has to be stronger for it.

`CapabilitySpec.protected` was a flag a capability set about ITSELF. That is the
wrong level: the same capability is load-bearing in an Aleph that serves requests
and optional in one running purely as a plugin host, and nothing on the
capability can know which deployment it is in.

A pin is the OPERATOR naming, in the boot manifest, a key this deployment exists
to serve. Two mechanisms now do what one flag did badly:

* **Unnameable, not refused.** `register_core` assigns no `PluginId`, so a
  manifest capability cannot be named by `deactivate`'s only argument. That was
  always true and `protected` merely restated it beside the real defence.
* **Pins**, which catch what the flag could not: retiring the SOLE PROVIDER of a
  pinned key is refused even when its collateral is empty, because nothing
  depends on it yet and the deployment still needs the key.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.context import Context
from aleph_kernel.errors import DependentsWouldBreak
from aleph_kernel.kernel import Kernel, PluginId
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok


def _spec(name: str, *, provides: tuple[str, ...] = (), requires: tuple[str, ...] = ()):
    async def setup(ctx: Context) -> AsyncIterator[None]:
        for key in provides:
            ctx.provide(key, f"{name}:{key}")
        if False:  # pragma: no cover
            yield

    async def probe(_ctx: Context) -> ProbeResult:
        return ok(name)

    return CapabilitySpec(
        name=name,
        setup=setup,
        probe=probe,
        provides=frozenset(provides),
        requires=frozenset(requires),
    )


async def test_manifest_capability_has_no_id_to_deactivate_with() -> None:
    """The primary defence, and it needs no flag: `register_core` mints nothing,
    so there is no value an agent could pass to reach core capability."""
    kernel = Kernel()
    kernel.register_core(_spec("database", provides=("db.sessions",)))
    await kernel.boot()

    api = AgentPluginAPI(kernel)
    views = {v.name: v for v in api.inspect()}
    assert views["database"].plugin_id is None
    assert views["database"].removable is False
    assert views["database"].protected is True, "derived from having no id, not from a flag"


async def test_retiring_the_sole_provider_of_a_pinned_key_is_refused() -> None:
    """What the flag could NOT express.

    Nothing depends on this plugin, so its collateral is empty and the old
    `protected_collateral` check would have waved it through. The deployment
    still declared it needs the key.
    """
    kernel = Kernel(pins=frozenset({"cache"}))
    api = AgentPluginAPI(kernel)
    outcome = await api.install(_spec("cache-plugin", provides=("cache",)))

    radius = kernel.blast_radius("cache-plugin")
    assert radius.collateral == frozenset(), "nothing depends on it"
    assert radius.pinned_collateral == {"cache"}
    assert radius.is_safe is False

    with pytest.raises(DependentsWouldBreak):
        await kernel.deactivate(PluginId(uuid.UUID(outcome.plugin_id)))


async def test_force_cannot_override_a_pin() -> None:
    """An operator may accept breaking their own plugins. Nobody may take away
    the thing the deployment declared it exists to serve."""
    kernel = Kernel(pins=frozenset({"cache"}))
    api = AgentPluginAPI(kernel)
    outcome = await api.install(_spec("cache-plugin", provides=("cache",)))

    with pytest.raises(DependentsWouldBreak):
        await kernel.deactivate(PluginId(uuid.UUID(outcome.plugin_id)), force=True)


async def test_a_pin_with_a_surviving_provider_is_not_violated() -> None:
    """The pin is about the KEY staying available, not about one provider
    surviving — so retiring a redundant provider reads as safe, which is what
    makes redundancy worth having."""
    kernel = Kernel(pins=frozenset({"cache"}))
    api = AgentPluginAPI(kernel)
    kernel.register_core(_spec("primary-cache", provides=("cache",)))
    await kernel.boot()
    outcome = await api.install(_spec("spare", provides=("cache-spare",)))

    radius = kernel.blast_radius("spare")
    assert radius.pinned_collateral == frozenset()
    await kernel.deactivate(PluginId(uuid.UUID(outcome.plugin_id)))


async def test_no_pins_means_an_operator_may_retire_anything() -> None:
    """Empty is a legitimate configuration, not a misconfiguration. A scratch
    deployment pins nothing — which the old flag could never express, because it
    lived on the capability rather than on the deployment."""
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    outcome = await api.install(_spec("anything", provides=("whatever",)))

    assert kernel.blast_radius("anything").is_safe is True
    await kernel.deactivate(PluginId(uuid.UUID(outcome.plugin_id)))
    assert "anything" not in kernel.active()


async def test_a_plugin_cannot_pin_itself() -> None:
    """Pins come from the manifest and are handed to the Kernel constructor.
    There is no API a plugin can reach to add one — which is the property
    `protected` needed a refusal for, and that pins get structurally."""
    kernel = Kernel(pins=frozenset({"cache"}))
    assert kernel.pins == {"cache"}
    assert not hasattr(kernel, "add_pin")
    assert not hasattr(AgentPluginAPI(kernel), "pin")
