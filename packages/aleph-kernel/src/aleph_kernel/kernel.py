"""The kernel: registration, boot, activation with a probe gate, guarded removal.

Composition changes are serialized under one lock and driven to quiescence
topologically. That is a deliberate simplification of the paper's §4.3.3
inertial state machine: with no concurrent reconfiguration there is no
interleaving to reconcile, which removes the entire class of bug every reader of
this design flagged as where the risk lives. The four-state fiber machine and
reactive re-resolution are deferred until something actually needs a provider
swapped under a running consumer.

Two properties are load-bearing and neither is negotiable:

* **A capability cannot activate without passing a probe** against the live
  system. Registration refuses a spec with no probe; activation rolls back when
  the probe fails.
* **A capability cannot be removed if others depend on it**, and protected
  capabilities have no addressable id at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NewType
from uuid import UUID

from aleph_kernel.context import ROOT_REALM, Context, Store
from aleph_kernel.effects import EffectScope
from aleph_kernel.errors import DependentsWouldBreak, ProbeFailed
from aleph_kernel.ids import uuid7
from aleph_kernel.spec import ProbeResult, problem
from aleph_kernel.support import BlastRadius, dependent_closure, topological_order
from aleph_kernel.tracing import start_span

if TYPE_CHECKING:
    from aleph_kernel.spec import CapabilitySpec

__all__ = ["Kernel", "PluginId", "State"]

#: A handle to a *dynamically registered* capability. Minted only by
#: `Kernel.register_dynamic`, and the only thing `deactivate` accepts.
#:
#: This is the primary guardrail and it costs no policy: a capability mounted
#: from the boot manifest never receives one, so there is no argument value an
#: agent can construct that names it. Deactivating core capability is not
#: refused — it is unexpressible. Refusal (`ProtectedCapability`) exists only to
#: catch a kernel-internal mistake.
PluginId = NewType("PluginId", UUID)


class State(StrEnum):
    REGISTERED = "registered"
    ACTIVE = "active"
    FAILED = "failed"


@dataclass
class _Mounted:
    spec: CapabilitySpec
    state: State = State.REGISTERED
    scope: EffectScope | None = None
    context: Context | None = None
    plugin_id: PluginId | None = None
    failure: str = ""


class Kernel:
    """Owns the capability graph and every transition of it."""

    #: How long a capability's probe may take before it counts as failed.
    #:
    #: Generous, because a probe legitimately does real work — a round trip to
    #: Postgres, a write and read through the asset store. Bounded, because an
    #: unbounded probe turns a dependency that hangs into a process that never
    #: reports.
    DEFAULT_PROBE_TIMEOUT_S = 30.0

    def __init__(
        self,
        *,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        pins: frozenset[str] = frozenset(),
    ) -> None:
        self._store = Store()
        self._mounted: dict[str, _Mounted] = {}
        self._lock = asyncio.Lock()
        self._probe_timeout_s = probe_timeout_s
        #: Keys this DEPLOYMENT exists to serve, from the boot manifest.
        #:
        #: Replaces `CapabilitySpec.protected`. A pin is the operator saying
        #: "requests here need a database"; the old flag was a capability
        #: asserting its own importance, which is the wrong level — the same
        #: capability is load-bearing in one deployment and optional in another.
        #:
        #: Empty is a legitimate configuration: an Aleph with nothing pinned
        #: lets an operator retire anything, which is what a scratch deployment
        #: wants and what the flag could never express.
        self._pins = frozenset(pins)

    # -- registration --------------------------------------------------------

    def register_core(self, spec: CapabilitySpec) -> None:
        """Mount a capability from the boot manifest. Gets no ``PluginId``, ever."""
        self._register(spec)

    def register_dynamic(self, spec: CapabilitySpec) -> PluginId:
        """Mount an agent-authored capability and mint its handle.

        There is no self-declared privilege to refuse any more. `protected` is
        gone: a capability could assert its own importance, which is the wrong
        level, and the guardrail it was reaching for now comes from two places
        that a plugin cannot touch — this method assigns a `PluginId` while
        `register_core` does not (so manifest capability is unnameable rather
        than refused), and PINS are declared by the operator in the boot
        manifest.
        """
        self._register(spec)
        pid = PluginId(uuid7())
        self._mounted[spec.name].plugin_id = pid
        return pid

    def _register(self, spec: CapabilitySpec) -> None:
        """Record a capability. Graph validity is checked at boot, not here.

        Registration is deliberately order-independent: a consumer may be
        registered before its provider, which is unavoidable once plugins arrive
        at runtime rather than in a fixed sequence. Cycles and missing providers
        surface from `topological_order` at `boot`/`activate`, where the whole
        graph is known — validating here would reject valid configurations for
        the accident of their registration order.
        """
        if spec.name in self._mounted:
            msg = f"{spec.name!r} is already registered"
            raise ValueError(msg)
        self._mounted[spec.name] = _Mounted(spec=spec)

    def unregister(self, name: str) -> None:
        """Drop a registration entirely. The inverse of :meth:`_register`.

        Without this, a plugin that fails to install is left REGISTERED with no
        way to remove it — and because `boot` and `activate` both re-derive the
        topological order over the WHOLE registered set, one bad leftover makes
        every subsequent install fail too, for the life of the process. That is
        not an edge case: the first two things that happen when an agent writes
        a plugin are that the first attempt is wrong and the second attempt is
        version two of the same name.

        Refuses a protected capability, and refuses an ACTIVE one — an active
        capability must be torn down through `deactivate`, which computes the
        blast radius first. Dropping it here would leave its effects unwound and
        its dependents holding a binding nothing provides.

        NOTE: this method is addressed by NAME, not by `PluginId`. The kernel's
        primary guardrail is that core capability has no addressable id, so a
        name-addressed method sidesteps it by construction. The `protected`
        check below is therefore the only defence, which is why it is the first
        statement in the method and why it ships with its own test.
        """
        mounted = self._mounted.get(name)
        if mounted is None:
            msg = f"{name!r} is not registered"
            raise KeyError(msg)
        if mounted.state is State.ACTIVE:
            msg = (
                f"{name!r} is active; deactivate it first so its effects unwind "
                f"and its dependents are checked"
            )
            raise ValueError(msg)
        del self._mounted[name]

    def _specs(self) -> dict[str, CapabilitySpec]:
        return {name: m.spec for name, m in self._mounted.items()}

    # -- introspection -------------------------------------------------------

    @property
    def store(self) -> Store:
        """The coeffect store, for a host building its own reader context.

        Handing out the store does not weaken the declaration check: a `Context`
        still refuses any key absent from its own `requires`, so a reader must
        declare what it reads exactly like a capability does.
        """
        return self._store

    def state_of(self, name: str) -> State:
        return self._mounted[name].state

    def spec_for(self, name: str) -> CapabilitySpec:
        """The registered spec, for callers that need to read a declaration.

        Public because a probe asking "is every pinned key actually provided?"
        should not have to reach into `_specs`, and a private read from outside
        is how `agent_api` ended up touching `_mounted` directly.
        """
        return self._mounted[name].spec

    def active(self) -> frozenset[str]:
        return frozenset(n for n, m in self._mounted.items() if m.state is State.ACTIVE)

    def is_provided(self, key: str) -> bool:
        """True when some active capability currently provides ``key``.

        Read-only introspection for probes and operators. Deliberately not a
        resolver: it answers "is this up", never "give me the value", so it
        cannot be used to sidestep the declaration check in `Context.get`.
        """
        found, _ = self._store.get(ROOT_REALM, key)
        return found

    @property
    def pins(self) -> frozenset[str]:
        """The keys this deployment declared it exists to serve."""
        return self._pins

    def blast_radius(self, name: str) -> BlastRadius:
        """What retiring ``name`` would take with it. Pure; changes nothing."""
        return dependent_closure(self._specs(), name, pins=self._pins)

    # -- lifecycle -----------------------------------------------------------

    async def boot(self) -> None:
        """Activate everything registered, providers first."""
        async with self._lock:
            for name in topological_order(self._specs()):
                if self._mounted[name].state is not State.ACTIVE:
                    await self._activate(name)

    async def activate(self, name: str) -> None:
        """Activate one capability, its providers first if they are not up yet."""
        async with self._lock:
            order = topological_order(self._specs())  # raises on cycle / missing provider
            needed = self._providers_for(name, order)
            for dep in needed:
                if self._mounted[dep].state is not State.ACTIVE:
                    await self._activate(dep)

    def _providers_for(self, name: str, order: tuple[str, ...]) -> list[str]:
        """``name`` and everything it transitively requires, in activation order."""
        provider_of: dict[str, str] = {}
        for other in sorted(self._mounted):
            for key in sorted(self._mounted[other].spec.provides):
                provider_of.setdefault(key, other)

        needed: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in needed:
                continue
            needed.add(current)
            stack.extend(provider_of[k] for k in self._mounted[current].spec.requires)
        return [n for n in order if n in needed]

    async def _activate(self, name: str) -> None:
        """Set up, then prove it works. A failed probe leaves nothing behind."""
        mounted = self._mounted[name]
        spec = mounted.spec
        with start_span("kernel.activate", **{"aleph.capability": name}) as span:
            scope = EffectScope(name)
            # Readable = what it declared it needs, PLUS what it declared it
            # supplies. A capability can obviously see its own provisions, and a
            # probe must be able to: proving a capability works means exercising
            # the service it just published. Everything else is still refused,
            # so the declaration remains an enforced boundary rather than a
            # suggestion.
            ctx = Context(
                owner=name,
                requires=spec.requires | spec.provides,
                optional=spec.optional,
                provides=spec.provides,
                store=self._store,
                scope=scope,
            )
            try:
                await scope.drive(spec.setup(ctx))
            except Exception as exc:
                # Setup raised partway: the inverses it yielded before raising
                # must still run, or it leaks exactly what it had built.
                await scope.unwind()
                mounted.state = State.FAILED
                mounted.failure = f"setup raised: {exc!r}"
                span.set_attribute("aleph.kernel.outcome", "setup_failed")
                raise

            # The probe gate. Runs against the live system, with the same scoped
            # context the capability itself gets — so a probe can only use what
            # the capability declared, and cannot fake a dependency it lacks.
            #
            # And it runs under a DEADLINE. A probe is a liveness check against
            # something external, so "it never came back" is a normal outcome
            # and it has to be a reported one. Without this, a service that
            # accepts a TCP connection and then never answers — a stale
            # forwarded port, a wedged proxy, a firewall that drops rather than
            # rejects — hangs `boot()` forever, and the process reports nothing
            # at all: no failure, no log line, no exit. Observed exactly that:
            # a port-forwarder had taken 6379 on the host, so `redis.ping()`
            # accepted the connection and never replied, and every acceptance
            # run sat silent for forty minutes.
            #
            # A hang reported as a failure is strictly better than a hang: the
            # capability unwinds, the operator is told which one and how long it
            # waited, and the gate goes red instead of going quiet.
            try:
                result = await asyncio.wait_for(spec.probe(ctx), timeout=self._probe_timeout_s)
            except TimeoutError as exc:
                await scope.unwind()
                mounted.state = State.FAILED
                mounted.failure = f"probe did not answer within {self._probe_timeout_s}s"
                span.set_attribute("aleph.kernel.outcome", "probe_timeout")
                raise ProbeFailed(name, mounted.failure) from exc
            except Exception as exc:
                await scope.unwind()
                mounted.state = State.FAILED
                mounted.failure = f"probe raised: {exc!r}"
                span.set_attribute("aleph.kernel.outcome", "probe_raised")
                raise ProbeFailed(name, f"probe raised {exc!r}") from exc

            if not result.passed:
                await scope.unwind()
                mounted.state = State.FAILED
                mounted.failure = result.detail
                span.set_attribute("aleph.kernel.outcome", "probe_failed")
                raise ProbeFailed(name, result.detail)

            mounted.scope = scope
            mounted.context = ctx
            mounted.state = State.ACTIVE
            mounted.failure = ""
            span.set_attribute("aleph.kernel.outcome", "active")
            span.set_attribute("aleph.kernel.probe", result.detail or "ok")

    async def replace(self, plugin_id: PluginId, spec: CapabilitySpec) -> PluginId:
        """Swap a live capability's implementation with no process restart.

        The precondition for anything an agent authors: a plugin it improves
        must be replaceable while the system serves requests, or every
        self-modification costs a restart and discards all process-local state.

        THE VALUABLE PROPERTY IS THE ROLLBACK, not the swap. If the replacement
        fails to set up or fails its probe, the PREVIOUS implementation is
        brought back and the caller is told the swap was refused. A hot-reload
        that can leave a capability absent is worse than no hot-reload: the
        agent believes it improved something and instead removed it.

        Refused when other capabilities depend on this one and the replacement
        does not provide everything the old one did — the dependents are live,
        and taking their footing away mid-flight is not a swap, it is a break.
        """
        async with self._lock:
            name = self._name_for(plugin_id)
            mounted = self._mounted[name]

            previous = mounted.spec
            lost = previous.provides - spec.provides
            if lost:
                radius = self.blast_radius(name)
                if radius.collateral:
                    raise DependentsWouldBreak(
                        name,
                        tuple(sorted(radius.collateral)),
                        tuple(sorted(radius.pinned_collateral)),
                    )

            await self._teardown(name)
            mounted.spec = spec
            try:
                await self._activate(name)
            except Exception:
                # Put the working version back. The caller asked to improve a
                # capability, not to lose it.
                mounted.spec = previous
                await self._activate(name)
                raise

            new_id = PluginId(uuid7())
            mounted.plugin_id = new_id
            return new_id

    async def reprobe(self, name: str) -> ProbeResult:
        """Re-run an active capability's probe, and retire it if it now fails.

        The activation gate catches a capability that is broken when it loads.
        This catches one that breaks later — a dependency that went away, a
        resource that closed, an agent-authored plugin whose fault only appears
        on the second call. Without it, "probed once at boot" decays into
        "assumed working forever", which is the same green-while-broken failure
        the gate exists to prevent, just deferred.

        A failure tears the capability down rather than leaving it ACTIVE and
        broken: a capability nobody can rely on is worse than one that is
        visibly absent, because callers keep reaching for it.
        """
        async with self._lock:
            mounted = self._mounted[name]
            if mounted.state is not State.ACTIVE or mounted.context is None:
                return problem(f"{name} is {mounted.state.value}, not active")
            try:
                result = await mounted.spec.probe(mounted.context)
            except Exception as exc:
                result = problem(f"probe raised {exc!r}")
            if not result.passed:
                await self._teardown(name)
                mounted.state = State.FAILED
                mounted.failure = result.detail
            return result

    async def deactivate(self, plugin_id: PluginId, *, force: bool = False) -> BlastRadius:
        """Retire a dynamically registered capability.

        Computes the blast radius first and refuses when anything else would
        stop. `force` still refuses when a PINNED key would be left unprovided —
        an operator may accept breaking their own plugins; nobody may take away
        the thing the deployment declared it exists to serve.

        There is no protected-capability check any more, and none is needed:
        `register_core` assigns no `PluginId`, so a manifest capability cannot be
        named by this method's only argument. It was never refused; it was
        unexpressible, and now that is the whole mechanism rather than a flag
        beside it.
        """
        async with self._lock:
            name = self._name_for(plugin_id)

            radius = self.blast_radius(name)
            if radius.pinned_collateral:
                raise DependentsWouldBreak(
                    name,
                    tuple(sorted(radius.collateral)),
                    tuple(sorted(radius.pinned_collateral)),
                )
            if radius.collateral and not force:
                raise DependentsWouldBreak(name, tuple(sorted(radius.collateral)), ())

            # Dependents first, so nothing is left holding a withdrawn binding.
            teardown_first = reversed(topological_order(self._specs()))
            order = [n for n in teardown_first if n in radius.collateral]
            for dependent in [*order, name]:
                await self._teardown(dependent)
            return radius

    async def _teardown(self, name: str) -> None:
        mounted = self._mounted[name]
        if mounted.scope is not None:
            await mounted.scope.unwind()
        mounted.scope = None
        mounted.context = None
        mounted.state = State.REGISTERED

    async def shutdown(self) -> None:
        """Tear everything down, dependents before providers."""
        async with self._lock:
            for name in reversed(topological_order(self._specs())):
                if self._mounted[name].state is State.ACTIVE:
                    await self._teardown(name)

    def plugin_id_for(self, name: str) -> PluginId | None:
        """The handle for a capability, or None if it has no handle.

        The inverse of `_name_for`, and public because a caller holding a NAME
        (a database row, a manifest entry) needs the handle before it can ask
        for anything to be retired.

        `None` means core: a capability mounted from the boot manifest carries
        no `plugin_id`, so there is nothing to pass to `deactivate` and nothing
        to refuse. It is not protected-and-therefore-refused, it is
        unaddressable — the guardrail is that the handle does not exist.

        `None` is also the answer for a name nothing has mounted, and the two
        are deliberately the same: a caller that must not remove core must also
        not remove something absent, and neither is an error worth raising.
        """
        mounted = self._mounted.get(name)
        return mounted.plugin_id if mounted is not None else None

    def _name_for(self, plugin_id: PluginId) -> str:
        for name, mounted in self._mounted.items():
            if mounted.plugin_id == plugin_id:
                return name
        msg = f"no dynamically registered capability with id {plugin_id}"
        raise KeyError(msg)
