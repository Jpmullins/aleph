"""The support set and blast radius — the guardrail, computed not tried."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from aleph_kernel.context import Context
from aleph_kernel.errors import CyclicDependency, MissingProvider
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok
from aleph_kernel.support import dependent_closure, support_set, topological_order


async def _noop_setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
    if False:  # pragma: no cover - a generator that yields nothing
        yield


async def _pass(ctx: Context) -> ProbeResult:
    return ok()


def spec(
    name: str,
    *,
    provides: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    protected: bool = False,
) -> CapabilitySpec:
    return CapabilitySpec(
        name=name,
        setup=_noop_setup,
        probe=_pass,
        provides=frozenset(provides),
        requires=frozenset(requires),
        protected=protected,
    )


def graph(*specs: CapabilitySpec) -> dict[str, CapabilitySpec]:
    return {s.name: s for s in specs}


# -- support set -------------------------------------------------------------


def test_everything_is_supported_when_nothing_is_retired() -> None:
    g = graph(spec("db", provides=("db",)), spec("api", requires=("db",)))
    assert support_set(g) == {"db", "api"}


def test_retiring_a_provider_drops_its_dependents() -> None:
    g = graph(spec("db", provides=("db",)), spec("api", requires=("db",)))
    assert support_set(g, retired=["db"]) == frozenset()


def test_collapse_is_transitive() -> None:
    """A three-deep chain must fall in full, not one level per pass."""
    g = graph(
        spec("db", provides=("db",)),
        spec("repo", provides=("repo",), requires=("db",)),
        spec("service", provides=("svc",), requires=("repo",)),
        spec("route", requires=("svc",)),
    )
    assert support_set(g, retired=["db"]) == frozenset()


def test_unrelated_capabilities_survive() -> None:
    g = graph(
        spec("db", provides=("db",)),
        spec("api", requires=("db",)),
        spec("clock", provides=("clock",)),
    )
    assert support_set(g, retired=["db"]) == {"clock"}


# -- blast radius ------------------------------------------------------------


def test_blast_radius_reports_transitive_collateral() -> None:
    g = graph(
        spec("db", provides=("db",)),
        spec("repo", provides=("repo",), requires=("db",)),
        spec("route", requires=("repo",)),
    )
    radius = dependent_closure(g, "db")
    assert radius.collateral == {"repo", "route"}
    assert not radius.is_safe
    assert "repo" in radius.describe()


def test_blast_radius_excludes_the_target_itself() -> None:
    g = graph(spec("db", provides=("db",)), spec("api", requires=("db",)))
    assert "db" not in dependent_closure(g, "db").collateral


def test_a_leaf_is_safe_to_retire() -> None:
    g = graph(spec("db", provides=("db",)), spec("api", requires=("db",)))
    radius = dependent_closure(g, "api")
    assert radius.is_safe
    assert "affects nothing else" in radius.describe()


def test_protected_collateral_is_called_out_separately() -> None:
    """Breaking your own plugin is a choice; breaking the kernel's footing is not."""
    g = graph(
        spec("db", provides=("db",)),
        spec("ledger", requires=("db",), protected=True),
        spec("scratch", requires=("db",)),
    )
    radius = dependent_closure(g, "db")
    assert radius.collateral == {"ledger", "scratch"}
    assert radius.protected_collateral == {"ledger"}
    assert "protected among them: ledger" in radius.describe()


def test_blast_radius_does_not_mutate_the_graph() -> None:
    g = graph(spec("db", provides=("db",)), spec("api", requires=("db",)))
    before = support_set(g)
    dependent_closure(g, "db")
    assert support_set(g) == before, "computing the radius changed the system"


# -- ordering ----------------------------------------------------------------


def test_providers_are_ordered_before_their_dependents() -> None:
    g = graph(
        spec("route", requires=("svc",)),
        spec("service", provides=("svc",), requires=("db",)),
        spec("db", provides=("db",)),
    )
    order = topological_order(g)
    assert order.index("db") < order.index("service") < order.index("route")


def test_order_is_deterministic() -> None:
    g = graph(spec("b", provides=("b",)), spec("a", provides=("a",)), spec("c", provides=("c",)))
    assert topological_order(g) == topological_order(g) == ("a", "b", "c")


def test_a_cycle_is_reported_not_deadlocked() -> None:
    """cordis leaves a cycle PENDING forever — indistinguishable from idle."""
    g = graph(
        spec("a", provides=("a",), requires=("b",)),
        spec("b", provides=("b",), requires=("a",)),
    )
    with pytest.raises(CyclicDependency) as exc:
        topological_order(g)
    assert set(exc.value.cycle) == {"a", "b"}


def test_a_missing_provider_fails_at_boot_not_at_first_use() -> None:
    g = graph(spec("api", requires=("db",)))
    with pytest.raises(MissingProvider, match="requires db"):
        topological_order(g)


def test_a_spec_without_a_usable_probe_is_refused() -> None:
    """The one field that cannot be defaulted away."""
    with pytest.raises(TypeError, match="prove it works"):
        CapabilitySpec(name="x", setup=_noop_setup, probe=None)  # type: ignore[arg-type]


def test_a_spec_cannot_both_provide_and_require_a_key() -> None:
    with pytest.raises(ValueError, match="both provides and requires"):
        CapabilitySpec(
            name="x",
            setup=_noop_setup,
            probe=_pass,
            provides=frozenset({"db"}),
            requires=frozenset({"db"}),
        )
