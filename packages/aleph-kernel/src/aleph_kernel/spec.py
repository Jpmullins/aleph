"""What a capability is.

A capability declares what it provides, what it requires, how to set itself up,
and — mandatorily — how to prove it works. There is no way to register one
without a probe, because the probe is the only field in this system that can
fail for a real reason.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aleph_kernel.context import Context

__all__ = ["CapabilitySpec", "Probe", "ProbeResult", "Setup", "ok", "problem"]


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of proving a capability works. ``detail`` is required on failure."""

    passed: bool
    detail: str = ""


def ok(detail: str = "") -> ProbeResult:
    return ProbeResult(passed=True, detail=detail)


def problem(detail: str) -> ProbeResult:
    """Fail a probe. The detail is mandatory and should name what was observed."""
    if not detail.strip():
        msg = "a failing probe must say what it observed"
        raise ValueError(msg)
    return ProbeResult(passed=False, detail=detail)


class Setup(Protocol):
    """Install a capability's effects.

    An async generator. Everything it yields is an inverse, run in LIFO order on
    teardown. Yielding is how a capability says "I have made this change to the
    world, and here is how to unmake it" in one lexical place — which is what
    stops a write path from being authored without its undo.

        async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
            pool = await open_pool()
            yield pool.close
            ctx.provide("db", pool)      # provide() registers its own inverse
    """

    def __call__(self, ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]: ...


class Probe(Protocol):
    """Prove the capability works, against the live system, after activation.

    Runs with a context scoped exactly as the capability's own, so a probe can
    only use what the capability declared. It must exercise the capability's
    READ path — the thing a caller would actually do — not assert that setup
    returned.

    A probe that cannot fail is worse than no probe: it converts an unexamined
    assumption into a green light. `assert len(result) > 0` against a fixture
    the probe itself created is the canonical example.
    """

    def __call__(self, ctx: Context) -> Awaitable[ProbeResult]: ...


@dataclass(frozen=True)
class CapabilitySpec:
    """A registrable capability.

    ``provides`` and ``requires`` are the coeffect declaration. They are read by
    the topological boot and by the support-set computation that answers "what
    breaks if this goes away" — so they must be accurate, and the kernel
    enforces that by mediating every context access against ``requires``.
    """

    name: str
    setup: Setup
    #: Proof that the capability works. Mandatory — there is no default, and
    #: `register()` rejects a spec whose probe is missing.
    probe: Probe
    provides: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()
    #: Set only by the boot manifest loader. Not settable from a plugin, and no
    #: API mutates it — see manifest.py for why that matters.
    protected: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "a capability needs a name"
            raise ValueError(msg)
        # The field has no default, so a typed caller cannot omit it. This
        # guards the untyped path — a spec built from a plugin manifest — where
        # `probe` could arrive as None or as something that is not callable.
        if not callable(self.probe):
            msg = (
                f"{self.name!r} has no usable probe. A capability must be able to "
                f"prove it works against the live system before it is allowed to load."
            )
            raise TypeError(msg)
        overlap = self.provides & self.requires
        if overlap:
            msg = f"{self.name!r} both provides and requires {', '.join(sorted(overlap))}"
            raise ValueError(msg)
