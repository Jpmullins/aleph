"""Capability doubles for exercising the kernel.

Shipped in the package rather than in a test directory so a manifest can name
them by an importable path — `mount_manifest` resolves `module:callable`, and a
test-only module is not importable from every context that needs to boot one.

Useful outside tests too: a smoke manifest that boots nothing real is the
quickest way to check the kernel itself is healthy in an environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok, problem

if TYPE_CHECKING:
    from aleph_kernel.context import Context

__all__ = ["always_fails", "consumer", "provider"]


def provider(name: str = "db", key: str = "db") -> CapabilitySpec:
    """A capability that publishes one service and proves it resolves."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        ctx.provide(key, f"{name}:{key}")
        if False:  # pragma: no cover - nothing to undo beyond the binding
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return ok(f"{key} resolves") if ctx.has(key) else problem(f"{key} did not resolve")

    return CapabilitySpec(name=name, setup=setup, probe=probe, provides=frozenset({key}))


def consumer(name: str = "reports", key: str = "db") -> CapabilitySpec:
    """A capability that requires one service and proves it can read it."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        ctx.get(key)
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return ok() if ctx.has(key) else problem(f"{key} unavailable")

    return CapabilitySpec(name=name, setup=setup, probe=probe, requires=frozenset({key}))


def always_fails(name: str = "broken") -> CapabilitySpec:
    """A capability whose probe never passes — for exercising the gate."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return problem("this capability exists to fail its probe")

    return CapabilitySpec(name=name, setup=setup, probe=probe)
