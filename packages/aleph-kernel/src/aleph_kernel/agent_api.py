"""What an agent may do to its own capability set.

The product thesis: an agent that authors plugins for itself and activates or
deactivates them as needed, under guardrails that stop it removing load-bearing
capability.

This module is the whole of that surface, deliberately. Four operations, each
of which fails closed:

* :meth:`AgentPluginAPI.install`   author a capability and prove it works
* :meth:`AgentPluginAPI.enable`    bring one up
* :meth:`AgentPluginAPI.disable`   take one down, if nothing depends on it
* :meth:`AgentPluginAPI.inspect`   see what exists and what removing it would cost

The guardrail is not a policy check inside `disable`. It is that `disable` takes
a ``PluginId``, `PluginId` values are minted only by `register_dynamic`, and
core capabilities are mounted from the boot manifest and never receive one.
An agent holding every id it has ever been given still cannot name the ledger.

`inspect` matters as much as the refusals. An agent that cannot see the blast
radius before acting will keep proposing removals that get refused, and a
refusal it cannot predict is indistinguishable from a broken tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aleph_kernel.errors import DependentsWouldBreak, ProbeFailed

if TYPE_CHECKING:
    from aleph_kernel.kernel import Kernel, PluginId
    from aleph_kernel.spec import CapabilitySpec

__all__ = ["AgentPluginAPI", "CapabilityView", "InstallOutcome"]


@dataclass(frozen=True)
class CapabilityView:
    """What an agent is told about one capability.

    ``plugin_id`` is None for anything the agent cannot address — which is how
    it learns that core capability is not merely protected but unnameable,
    without being handed a value it could try.
    """

    name: str
    state: str
    protected: bool
    provides: tuple[str, ...]
    requires: tuple[str, ...]
    plugin_id: str | None
    removable: bool
    would_also_stop: tuple[str, ...]


@dataclass(frozen=True)
class InstallOutcome:
    installed: bool
    plugin_id: str | None
    detail: str


class AgentPluginAPI:
    """The agent's view of the kernel. Nothing else is exposed."""

    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    def inspect(self) -> list[CapabilityView]:
        """Every capability, with the cost of removing it precomputed.

        The blast radius is a pure function over the declaration graph, so
        showing it costs nothing and changes nothing.
        """
        views: list[CapabilityView] = []
        for name in sorted(self._kernel._mounted):
            mounted = self._kernel._mounted[name]
            radius = self._kernel.blast_radius(name)
            addressable = mounted.plugin_id is not None and not mounted.spec.protected
            views.append(
                CapabilityView(
                    name=name,
                    state=mounted.state.value,
                    protected=mounted.spec.protected,
                    provides=tuple(sorted(mounted.spec.provides)),
                    requires=tuple(sorted(mounted.spec.requires)),
                    plugin_id=str(mounted.plugin_id) if addressable else None,
                    removable=addressable and radius.is_safe,
                    would_also_stop=tuple(sorted(radius.collateral)),
                )
            )
        return views

    async def install(self, spec: CapabilitySpec) -> InstallOutcome:
        """Register and activate an agent-authored capability.

        The probe gate is the admission test: setup runs, then the capability
        must prove it works against the live system. A failure unwinds it
        completely and reports why, so the agent gets a diagnosis rather than a
        half-mounted capability it believes is running.

        Nothing here is a "probation period" yet — the capability is either
        working and mounted or unwound and absent. That is a stronger guarantee
        than probation, and the weaker one is only needed for failures that
        appear later than activation.
        """
        try:
            plugin_id = self._kernel.register_dynamic(spec)
        except ValueError as exc:
            return InstallOutcome(installed=False, plugin_id=None, detail=str(exc))

        try:
            await self._kernel.activate(spec.name)
        except ProbeFailed as exc:
            return InstallOutcome(
                installed=False,
                plugin_id=None,
                detail=f"refused: {exc.detail}",
            )
        except Exception as exc:  # setup raised; the kernel already unwound it
            return InstallOutcome(
                installed=False, plugin_id=None, detail=f"refused: setup failed — {exc!r}"
            )
        return InstallOutcome(
            installed=True, plugin_id=str(plugin_id), detail=f"{spec.name} active and probed"
        )

    async def enable(self, name: str) -> InstallOutcome:
        """Bring a registered capability up, its providers first."""
        try:
            await self._kernel.activate(name)
        except ProbeFailed as exc:
            return InstallOutcome(installed=False, plugin_id=None, detail=f"refused: {exc.detail}")
        return InstallOutcome(installed=True, plugin_id=None, detail=f"{name} active")

    async def disable(self, plugin_id: PluginId, *, force: bool = False) -> InstallOutcome:
        """Take down an agent-installed capability.

        Refused when anything else depends on it. ``force`` accepts breaking
        other agent-installed plugins; it can never reach protected capability,
        because protected capability has no id to pass here in the first place.
        """
        try:
            radius = await self._kernel.deactivate(plugin_id, force=force)
        except DependentsWouldBreak as exc:
            return InstallOutcome(
                installed=True, plugin_id=str(plugin_id), detail=f"refused: {exc}"
            )
        except KeyError:
            return InstallOutcome(
                installed=False,
                plugin_id=None,
                detail=(
                    "no such plugin. Core capabilities are mounted from the boot "
                    "manifest and have no id — they cannot be disabled."
                ),
            )
        return InstallOutcome(
            installed=False,
            plugin_id=None,
            detail=(
                f"disabled; also stopped {', '.join(sorted(radius.collateral))}"
                if radius.collateral
                else "disabled; nothing else affected"
            ),
        )
