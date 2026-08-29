"""The support set — paper Definition 67, Lemma 68, Lemma 70.

A pure function from a configuration to *what will still be running*. No side
effects, no I/O, no mutation of the live system: it answers "what happens if I
retire this" by computing, not by trying.

This is the guardrail. The owner's requirement — "guardrails should prevent
deleting important features" — reduces to running this before a deactivation
and refusing when the answer contains something that must not stop. Because it
is pure, the refusal costs nothing and leaves the system untouched, which is
what makes it safe to expose to an agent.

cordis has the declarations this needs and never computes it (paper §6.5 notes a
runtime *could* report a cycle from the declarations alone; cordis does not).
Computing it is the single most valuable thing this kernel does that the
reference implementation does not.

Lemma 68 guarantees the well-founded recursion has exactly one solution, and
Lemma 70 identifies it with the ACTIVE set at quiescence — so the fixed point
below is not an approximation of what the runtime would do, it *is* what the
runtime will do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from aleph_kernel.spec import CapabilitySpec

__all__ = ["BlastRadius", "dependent_closure", "support_set", "topological_order"]


def support_set(
    specs: Mapping[str, CapabilitySpec],
    *,
    retired: Iterable[str] = (),
) -> frozenset[str]:
    """Names that remain supported once ``retired`` is removed.

    A capability is supported iff it is not retired and every key it requires is
    provided by some supported capability. Computed as the greatest fixed point:
    start optimistic, drop anything unsatisfied, repeat until stable. Lemma 68
    makes that solution unique.
    """
    retired_set = set(retired)
    alive = {name for name in specs if name not in retired_set}

    changed = True
    while changed:
        changed = False
        provided: set[str] = set()
        for name in alive:
            provided |= specs[name].provides
        for name in sorted(alive):
            if not specs[name].requires <= provided:
                alive.discard(name)
                changed = True
                break  # provided is now stale; recompute
    return frozenset(alive)


@dataclass(frozen=True)
class BlastRadius:
    """What retiring one capability would take with it.

    ``collateral`` excludes the target itself — it is exactly the set of things
    that would stop working as a consequence, which is what a human or an agent
    needs to see before deciding.
    """

    target: str
    collateral: frozenset[str]
    #: Pinned KEYS that would lose their provider.
    #:
    #: This was `protected_collateral` — capabilities carrying a flag they set
    #: about themselves. A pin is the opposite: the OPERATOR naming a key this
    #: deployment exists to serve, in the boot manifest. It says something about
    #: the deployment rather than about the capability, which is the level the
    #: claim belongs at — a database is load-bearing in an Aleph that serves
    #: requests and not in one running purely as a plugin host, and no flag on
    #: the capability can know which it is in.
    pinned_collateral: frozenset[str]

    @property
    def is_safe(self) -> bool:
        """True when nothing else stops and no pinned key loses its provider."""
        return not self.collateral and not self.pinned_collateral

    def describe(self) -> str:
        if self.is_safe:
            return f"retiring {self.target!r} affects nothing else"
        parts = [f"retiring {self.target!r} would also stop: {', '.join(sorted(self.collateral))}"]
        if self.pinned_collateral:
            parts.append(
                f"and would leave these pinned key(s) unprovided: "
                f"{', '.join(sorted(self.pinned_collateral))}"
            )
        return "; ".join(parts)


def dependent_closure(
    specs: Mapping[str, CapabilitySpec],
    target: str,
    *,
    pins: frozenset[str] = frozenset(),
) -> BlastRadius:
    """Compute the blast radius of retiring ``target``, without touching anything.

    Transitive by construction: the fixed point drops a capability whose
    provider vanished, then drops that one's dependents on the next pass, so a
    three-deep chain is reported in full rather than one level at a time.
    """
    if target not in specs:
        msg = f"unknown capability {target!r}"
        raise KeyError(msg)

    before = support_set(specs)
    after = support_set(specs, retired=[target])
    collateral = (before - after) - {target}

    # A pin is violated when NOTHING still standing provides it. The target
    # itself counts as leaving, so a capability that is the sole provider of a
    # pinned key cannot be retired even though its collateral may be empty —
    # which is the case a capability-level flag could not express.
    surviving = after - {target}
    still_provided: set[str] = set()
    for name in surviving:
        still_provided |= specs[name].provides
    pinned = frozenset(key for key in pins if key not in still_provided)

    return BlastRadius(
        target=target,
        collateral=frozenset(collateral),
        pinned_collateral=pinned,
    )


def topological_order(specs: Mapping[str, CapabilitySpec]) -> tuple[str, ...]:
    """Activation order: providers before the capabilities that require them.

    Raises ``CyclicDependency`` rather than leaving a cycle to deadlock. cordis
    lets a cycle sit as two fibers PENDING forever, which is indistinguishable
    from "loaded and idle" — the worst possible failure mode for something an
    agent authored and believes it installed.

    Ties break on name so boot order is deterministic and a failure reproduces.
    """
    from aleph_kernel.errors import AmbiguousProvider, CyclicDependency, MissingProvider

    providers: dict[str, str] = {}
    for name in sorted(specs):
        for key in sorted(specs[name].provides):
            # REFUSED, not resolved. This was `setdefault`, which meant two
            # capabilities providing one key silently elected the
            # alphabetically-first as the boot-order parent while the store
            # binding went to whichever activated LAST — so the graph and the
            # runtime disagreed about who supplied a service, with nothing
            # anywhere saying so. cordis has the same behaviour and it is the
            # one place Aleph should not copy it: an agent that installs a
            # plugin shadowing `db.sessions` deserves an error, not a coin toss.
            if key in providers and providers[key] != name:
                raise AmbiguousProvider(key, (providers[key], name))
            providers[key] = name

    for name in sorted(specs):
        missing = frozenset(k for k in specs[name].requires if k not in providers)
        if missing:
            raise MissingProvider(name, missing)

    # Depth-first with an explicit colour map so a cycle can be reported as the
    # actual path rather than "there is a cycle somewhere".
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(specs, WHITE)
    order: list[str] = []
    path: list[str] = []

    def visit(name: str) -> None:
        if colour[name] == BLACK:
            return
        if colour[name] == GREY:
            cycle = tuple(path[path.index(name) :])
            raise CyclicDependency(cycle)
        colour[name] = GREY
        path.append(name)
        for key in sorted(specs[name].requires):
            visit(providers[key])
        path.pop()
        colour[name] = BLACK
        order.append(name)

    for name in sorted(specs):
        visit(name)
    return tuple(order)
