"""A catalog is a kernel capability, and a pane declares that it needs one.

This is the half of WS-A3b that closes back onto the kernel. Everything in
:mod:`aleph_a2ui.plugin_catalogs` is a pure computation over names; nothing
there can stop an agent disabling a plugin whose components a pane on screen is
currently painting with. The kernel already can — it refuses a deactivation
whose blast radius is non-empty (`aleph_kernel.support.dependent_closure`) —
but only over declarations that exist.

So:

* a catalog capability **provides** ``ui:catalog:<catalog id>``
* a pinned pane capability **requires** the key of every catalog its surfaces
  name

and `disable(plugin)` on a plugin whose catalog a pane requires comes back
refused, naming the pane. Not by a policy check written next to `disable` — by
the same support-set computation that protects the ledger. That is the point:
the guardrail is a property of the declaration graph, so it holds for
declarations nobody anticipated.

**The pane's `requires` is load-bearing, not documentation.** Its probe reads
each catalog back through ``ctx.get``, and `Context.get` refuses a key the
capability did not declare (`UndeclaredAccess`, Algorithm 6). Delete the
`requires` and the pane does not merely lose its protection — it fails to
activate. A declaration that is only read by a graph walk decays into a comment
the first time someone edits it; one the code path itself depends on cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aleph_a2ui.plugin_catalogs import (
    MINIMUM_RENDERABLE,
    AssembledCatalog,
    catalog_capability_key,
)
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok, problem

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from aleph_kernel.context import Context

__all__ = ["PinnedPane", "catalog_capability", "pane_capability", "pane_capability_name"]


@dataclass(frozen=True)
class PinnedPane:
    """A pane held open in the workspace, and what it paints with."""

    pane_id: str
    #: Catalog ids its surfaces name in `createSurface`.
    catalog_ids: tuple[str, ...]
    #: Components its surface tree actually uses. Checked by the probe against
    #: the catalogs, so a pane pinned against a catalog that cannot draw it is
    #: refused at activation instead of rendering blank.
    components: tuple[str, ...] = ()


def pane_capability_name(pane_id: str) -> str:
    return f"ui:pane:{pane_id}"


def catalog_capability(catalog: AssembledCatalog) -> CapabilitySpec:
    """Publish one assembled catalog into the kernel graph.

    The probe is a read of the thing itself, not of the fact that setup ran: a
    catalog that cannot resolve ``Column`` and ``Text`` cannot paint any surface
    Aleph emits, so it must not come up claiming it can. `MINIMUM_RENDERABLE` is
    a real floor rather than `len(components) > 0`, which is the
    fixture-shaped probe `aleph_kernel.spec.Probe` warns about by name.
    """
    key = catalog_capability_key(catalog.catalog_id)

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        ctx.provide(key, catalog)
        if False:  # pragma: no cover - the binding's withdrawal is the inverse
            yield

    async def probe(ctx: Context) -> ProbeResult:
        live: AssembledCatalog = ctx.get(key)
        missing = sorted(MINIMUM_RENDERABLE - set(live.components))
        if missing:
            return problem(
                f"{live.catalog_id} cannot resolve {', '.join(missing)}; no Aleph surface "
                f"can be painted from it"
            )
        return ok(f"{len(live.components)} components, {len(live.functions)} functions")

    return CapabilitySpec(name=key, setup=setup, probe=probe, provides=frozenset({key}))


def pane_capability(pane: PinnedPane) -> CapabilitySpec:
    """A pinned pane, declared as depending on the catalogs it paints with.

    ``requires`` is what makes `disable` refuse. The probe is what makes
    ``requires`` real: it resolves every declared catalog and checks the pane's
    components are in one of them, so a pane wired to the wrong catalog is
    refused at activation rather than discovered as an empty rectangle.
    """
    keys = frozenset(catalog_capability_key(cid) for cid in pane.catalog_ids)
    name = pane_capability_name(pane.pane_id)

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        ctx.provide(name, pane)
        if False:  # pragma: no cover - the binding's withdrawal is the inverse
            yield

    async def probe(ctx: Context) -> ProbeResult:
        if not pane.catalog_ids:
            return problem(f"pane {pane.pane_id!r} names no catalog; it can paint nothing")
        resolvable: set[str] = set()
        for cid in pane.catalog_ids:
            # Refused outright if `requires` no longer carries this key. That is
            # the mutation this design is built to make loud.
            catalog: AssembledCatalog = ctx.get(catalog_capability_key(cid))
            resolvable |= set(catalog.components)
        unresolved = sorted(set(pane.components) - resolvable)
        if unresolved:
            return problem(
                f"pane {pane.pane_id!r} paints {', '.join(unresolved)}, which none of "
                f"{', '.join(pane.catalog_ids)} defines"
            )
        return ok(f"pane {pane.pane_id} resolves against {len(pane.catalog_ids)} catalog(s)")

    return CapabilitySpec(
        name=name, setup=setup, probe=probe, provides=frozenset({name}), requires=keys
    )
