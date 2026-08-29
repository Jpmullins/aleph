"""Kernel errors.

Each one names a distinct refusal. They are separate types because the caller's
correct response differs: an undeclared access is a bug in the capability, a
protected deactivation is a bug in the caller, and a failed probe is a bug in
the thing being activated.
"""

from __future__ import annotations

__all__ = [
    "CyclicDependency",
    "DependentsWouldBreak",
    "InactiveAccess",
    "KernelError",
    "MissingProvider",
    "ProbeFailed",
    "ProtectedCapability",
    "UndeclaredAccess",
]


class KernelError(Exception):
    """Base for every kernel refusal."""


class UndeclaredAccess(KernelError):
    """A capability reached for a service it never declared in ``requires``.

    Paper Algorithm 6: the resolution walk reached the root without finding a
    declaration. This is capability security — a plugin cannot touch what it did
    not ask for, which is what makes running agent-authored code survivable.
    """

    def __init__(self, capability: str, key: str) -> None:
        super().__init__(
            f"{capability!r} accessed {key!r} without declaring it. "
            f"Add {key!r} to its `requires` — reaching for an undeclared service "
            f"is refused rather than resolved."
        )
        self.capability = capability
        self.key = key


class InactiveAccess(KernelError):
    """A declared service exists but its provider is not ACTIVE.

    Paper Algorithm 6: the walk found a fiber that declares the key without
    having committed it. Distinct from ``UndeclaredAccess`` because the
    capability did nothing wrong — its dependency is mid-transition.
    """

    def __init__(self, capability: str, key: str) -> None:
        super().__init__(f"{capability!r} accessed {key!r} while its provider is not active")
        self.capability = capability
        self.key = key


class MissingProvider(KernelError):
    """A capability requires a service nothing provides. Refused at boot, not at first use."""

    def __init__(self, capability: str, keys: frozenset[str]) -> None:
        missing = ", ".join(sorted(keys))
        super().__init__(f"{capability!r} requires {missing}, which nothing provides")
        self.capability = capability
        self.keys = keys


class CyclicDependency(KernelError):
    """Two or more capabilities require each other.

    Reported at registration rather than left to deadlock. cordis leaves a cycle
    as two fibers PENDING forever, which for an agent-authored plugin is the
    worst available failure mode: indistinguishable from "loaded and idle".
    """

    def __init__(self, cycle: tuple[str, ...]) -> None:
        super().__init__("dependency cycle: " + " -> ".join([*cycle, cycle[0]]))
        self.cycle = cycle


class ProbeFailed(KernelError):
    """A capability's probe did not pass against the live system.

    The probe runs after activation and before the capability is considered
    ACTIVE. Failure rolls the activation back. This is the gate that makes
    "write side without read side" structurally hard: a capability must
    demonstrate its read path works, on the running system, or it does not load.
    """

    def __init__(self, capability: str, detail: str) -> None:
        super().__init__(f"probe for {capability!r} failed: {detail}")
        self.capability = capability
        self.detail = detail


class ProtectedCapability(KernelError):
    """Someone tried to deactivate a capability mounted from the boot manifest.

    Should be unreachable through the agent-facing API, which cannot name a
    protected capability at all — ``PluginId`` values are minted only by the
    dynamic registry. This exists to catch a kernel-internal mistake, not to be
    the primary guard.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"{name!r} is protected by the boot manifest and cannot be deactivated")
        self.name = name


class DependentsWouldBreak(KernelError):
    """Deactivation refused: live capabilities depend on this one.

    Computed from the support set (paper Definition 67) BEFORE anything is torn
    down, so the refusal costs nothing and leaves the system untouched.
    """

    def __init__(self, name: str, dependents: tuple[str, ...], protected: tuple[str, ...]) -> None:
        detail = ", ".join(dependents)
        because = f" including protected {', '.join(protected)}" if protected else ""
        super().__init__(
            f"deactivating {name!r} would take down {len(dependents)} "
            f"capabilit{'y' if len(dependents) == 1 else 'ies'}: {detail}{because}"
        )
        self.name = name
        self.dependents = dependents
        self.protected = protected


class UndeclaredProvide(KernelError):
    """A capability published a key it never declared it provides.

    The refusal matters more than it looks. `Context.provide` also registers the
    WITHDRAWAL of the binding on the publisher's own scope, so an undeclared
    publish did not merely add a wrong value — the publisher's teardown then
    deleted whichever binding was live, including the real one. Reads were
    mediated from the start; this closes the write side.
    """

    def __init__(self, owner: str, key: str) -> None:
        super().__init__(
            f"{owner!r} tried to provide {key!r}, which is not in its declared "
            f"`provides`. Add it to the spec, or publish under a key you own."
        )
        self.owner = owner
        self.key = key


class AmbiguousProvider(KernelError):
    """Two capabilities provide the same key.

    Previously resolved by `setdefault` over a sorted list: the boot order was
    computed against the alphabetically-first provider while the live binding
    belonged to whichever activated last. Those are not always the same
    capability, and nothing reported the disagreement — a plugin could shadow a
    core service and the only symptom would be the wrong object at a call site.
    """

    def __init__(self, key: str, providers: tuple[str, ...]) -> None:
        names = ", ".join(sorted(providers))
        super().__init__(
            f"{key!r} is provided by more than one capability ({names}). "
            f"Two providers of one key make the boot order and the live binding "
            f"disagree; rename one, or have one require the other."
        )
        self.key = key
        self.providers = tuple(sorted(providers))
