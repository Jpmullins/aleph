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

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aleph_kernel.errors import DependentsWouldBreak, ProbeFailed, ProtectedCapability
from aleph_kernel.kernel import State

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
    #: True for capability an agent cannot name — i.e. anything mounted from the
    #: boot manifest, which gets no `PluginId`. Derived from that fact rather
    #: than from a flag a capability set about itself.
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
            # `plugin_id is not None` is the WHOLE test now. `register_core`
            # assigns none, so manifest capability has no id an agent could
            # pass — it is unnameable rather than refused, which is what the
            # docstring above always claimed and what `protected` merely
            # restated beside it.
            addressable = mounted.plugin_id is not None
            views.append(
                CapabilityView(
                    name=name,
                    state=mounted.state.value,
                    protected=mounted.plugin_id is None,
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
            # Unregister, or the refused spec stays in the graph forever. Every
            # later `activate` re-derives the topological order over the whole
            # registered set, so one ghost makes every subsequent install fail
            # too — the agent's first wrong attempt would poison the process.
            self._unregister_quietly(spec.name)
            return InstallOutcome(
                installed=False,
                plugin_id=None,
                detail=f"refused: {exc.detail}",
            )
        except Exception as exc:  # setup raised; the kernel already unwound it
            self._unregister_quietly(spec.name)
            return InstallOutcome(
                installed=False, plugin_id=None, detail=f"refused: setup failed — {exc!r}"
            )
        return InstallOutcome(
            installed=True, plugin_id=str(plugin_id), detail=f"{spec.name} active and probed"
        )

    def _unregister_quietly(self, name: str) -> None:
        """Best-effort removal of a registration that never came up.

        Deliberately swallows: this runs on a path that is already returning a
        refusal, and a second exception here would replace the diagnosis the
        agent needs with one about cleanup. A capability that somehow came up
        anyway is refused by `unregister` itself and stays — which is correct.
        """
        with contextlib.suppress(KeyError, ValueError, ProtectedCapability):
            self._kernel.unregister(name)

    async def enable(self, name: str) -> InstallOutcome:
        """Bring a registered capability up, its providers first."""
        try:
            await self._kernel.activate(name)
        except ProbeFailed as exc:
            return InstallOutcome(installed=False, plugin_id=None, detail=f"refused: {exc.detail}")
        return InstallOutcome(installed=True, plugin_id=None, detail=f"{name} active")

    async def check_health(self) -> dict[str, str]:
        """Re-probe every agent-installed capability; retire the ones that fail.

        Probation, expressed as a recurring check rather than a timer. A plugin
        that passed at activation and fails later is retired automatically —
        the agent does not have to notice, and a broken capability does not sit
        ACTIVE while callers keep reaching for it.

        Core capability is deliberately NOT re-probed here. This is the agent's
        surface, and a probe is a live read against a running system; letting an
        agent trigger repeated reads of the ledger or the asset store from an
        untrusted loop is a denial-of-service it should not have. Core health is
        the operator's business.

        Returns ``{name: detail}`` for everything checked.
        """
        report: dict[str, str] = {}
        for name in sorted(self._kernel._mounted):
            mounted = self._kernel._mounted[name]
            if mounted.plugin_id is None:
                continue
            if mounted.state is not State.ACTIVE:
                report[name] = f"not active ({mounted.state.value})"
                continue
            result = await self._kernel.reprobe(name)
            report[name] = "ok" if result.passed else f"retired: {result.detail}"
        return report

    async def disable(
        self, plugin_id: PluginId, *, force: bool = False, unregister: bool = False
    ) -> InstallOutcome:
        """Take down an agent-installed capability.

        Refused when anything else depends on it. ``force`` accepts breaking
        other agent-installed plugins; it can never reach protected capability,
        because protected capability has no id to pass here in the first place.

        ``unregister`` picks between the two things "disable" can reasonably
        mean. Default False is **stopped but still installed**: the spec stays in
        the graph and `replace` still works on the handle, which is what an agent
        iterating on its own plugin wants. True is **gone**: the registration is
        dropped, and installing the same name again is a fresh install rather
        than `"'mine' is already registered"`.

        The returned ``plugin_id`` is the one that was handed in. It used to be
        None on success, so an agent that disabled its own plugin lost the only
        handle it had for it — `_teardown` leaves `plugin_id` intact and
        `replace` works fine on a disabled plugin, so the handle existed and the
        agent was simply never given it back.
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

        detail = (
            f"disabled; also stopped {', '.join(sorted(radius.collateral))}"
            if radius.collateral
            else "disabled; nothing else affected"
        )
        if unregister:
            # Dependents came down with it, so they are droppable too — leaving
            # them registered would keep the same ghost this flag exists to
            # avoid, one level out.
            for name in [*sorted(radius.collateral), self._kernel._name_for(plugin_id)]:
                self._unregister_quietly(name)
            return InstallOutcome(installed=False, plugin_id=None, detail=f"{detail}; unregistered")
        return InstallOutcome(installed=False, plugin_id=str(plugin_id), detail=detail)
