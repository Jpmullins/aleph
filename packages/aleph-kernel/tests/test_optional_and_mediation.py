"""Optional dependencies, write mediation, and the duplicate-provider refusal.

Three holes closed together because they are the same shape: a declaration the
kernel accepted and then did not enforce, or enforced in one direction only.

**Optional** existed nowhere. `Context.has()` returns False for an undeclared
key, so a genuinely optional dependency had to sit in `requires` to be readable
— and once there, `support_set` dropped the capability the moment that key went
unprovided. Asking "is scholar up?" cost you the ability to survive the answer.

**Write mediation** was absent. Reads were checked against `requires` from the
beginning; `provide()` accepted any key from any owner. Worse than a wrong
value: `provide` also registers the WITHDRAWAL on the publisher's own scope, so
an undeclared publish meant the publisher's teardown deleted whichever binding
was live — including the real one.

**Duplicate providers** were resolved by `setdefault` over a sorted list. The
boot order was computed against the alphabetically-first provider while the
store binding went to whichever activated last. Those are different
capabilities, and nothing reported it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from aleph_kernel.context import Context, Store
from aleph_kernel.effects import EffectScope
from aleph_kernel.errors import AmbiguousProvider, UndeclaredAccess, UndeclaredProvide
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok
from aleph_kernel.support import dependent_closure, support_set, topological_order


def _spec(
    name: str,
    *,
    provides: frozenset[str] = frozenset(),
    requires: frozenset[str] = frozenset(),
    optional: frozenset[str] = frozenset(),
) -> CapabilitySpec:
    async def setup(ctx: Context) -> AsyncIterator[None]:
        for key in sorted(provides):
            ctx.provide(key, f"{name}:{key}")
        if False:  # pragma: no cover
            yield

    async def probe(_ctx: Context) -> ProbeResult:
        return ok(name)

    return CapabilitySpec(
        name=name, setup=setup, probe=probe, provides=provides, requires=requires, optional=optional
    )


# -- optional ---------------------------------------------------------------


def test_an_optional_key_is_readable_when_present() -> None:
    store, scope = Store(), EffectScope("p")
    Context(
        owner="p", requires=frozenset(), provides=frozenset({"scholar"}), store=store, scope=scope
    ).provide("scholar", "SVC")

    ctx = Context(
        owner="reviewer",
        requires=frozenset(),
        optional=frozenset({"scholar"}),
        store=store,
        scope=EffectScope("r"),
    )
    assert ctx.has("scholar") is True
    assert ctx.get("scholar") == "SVC"


def test_an_optional_key_that_is_absent_answers_false_rather_than_raising() -> None:
    """The question the feature exists to let a capability ask."""
    ctx = Context(
        owner="reviewer",
        requires=frozenset(),
        optional=frozenset({"scholar"}),
        store=Store(),
        scope=EffectScope("r"),
    )
    assert ctx.has("scholar") is False


def test_an_undeclared_key_is_still_refused() -> None:
    """Optional widens what is DECLARED, not what is reachable."""
    store = Store()
    Context(
        owner="p",
        requires=frozenset(),
        provides=frozenset({"secret"}),
        store=store,
        scope=EffectScope("p"),
    ).provide("secret", "S")
    ctx = Context(
        owner="c",
        requires=frozenset(),
        optional=frozenset({"scholar"}),
        store=store,
        scope=EffectScope("c"),
    )
    assert ctx.has("secret") is False
    with pytest.raises(UndeclaredAccess):
        ctx.get("secret")


def test_an_optional_edge_does_not_drop_the_dependent() -> None:
    """The whole point. Required would drop it; optional must not."""
    specs = {
        "scholar": _spec("scholar", provides=frozenset({"scholar"})),
        "reviewer": _spec("reviewer", optional=frozenset({"scholar"})),
    }
    assert "reviewer" in support_set(specs, retired=["scholar"])

    hard = {
        "scholar": _spec("scholar", provides=frozenset({"scholar"})),
        "reviewer": _spec("reviewer", requires=frozenset({"scholar"})),
    }
    assert "reviewer" not in support_set(hard, retired=["scholar"])


def test_an_optional_edge_is_invisible_to_the_blast_radius() -> None:
    """Retiring scholar must not report the reviewer as collateral — it keeps
    working, returning zero findings, which is what the code already did."""
    specs = {
        "scholar": _spec("scholar", provides=frozenset({"scholar"})),
        "reviewer": _spec("reviewer", optional=frozenset({"scholar"})),
    }
    assert "reviewer" not in dependent_closure(specs, "scholar").collateral


def test_an_optional_edge_does_not_constrain_boot_order() -> None:
    """This is what lets the assistant start before the wiki and gain wiki tools
    when it arrives — no reordering, no deferred init, no null checks."""
    specs = {
        "assistant": _spec("assistant", provides=frozenset({"a"}), optional=frozenset({"wiki"})),
        "wiki": _spec("wiki", provides=frozenset({"wiki"}), requires=frozenset({"a"})),
    }
    order = topological_order(specs)
    assert order.index("assistant") < order.index("wiki")


def test_a_key_cannot_be_both_required_and_optional() -> None:
    with pytest.raises(ValueError, match="both required and optional"):
        _spec("x", requires=frozenset({"k"}), optional=frozenset({"k"}))


def test_a_capability_cannot_optionally_require_what_it_provides() -> None:
    with pytest.raises(ValueError, match="provides and optionally requires"):
        _spec("x", provides=frozenset({"k"}), optional=frozenset({"k"}))


# -- write mediation --------------------------------------------------------


def test_publishing_an_undeclared_key_is_refused() -> None:
    ctx = Context(
        owner="hijacker",
        requires=frozenset(),
        provides=frozenset({"mine"}),
        store=Store(),
        scope=EffectScope("h"),
    )
    ctx.provide("mine", "ok")
    with pytest.raises(UndeclaredProvide, match=r"db\.sessions"):
        ctx.provide("db.sessions", "HIJACKED")


def test_the_refusal_protects_the_withdrawal_too() -> None:
    """The dangerous half. `provide` registers the binding's REMOVAL on the
    publisher's scope, so an undeclared publish meant the publisher's teardown
    deleted the genuine binding — not merely shadowed it."""
    store = Store()
    real = Context(
        owner="database",
        requires=frozenset(),
        provides=frozenset({"db.sessions"}),
        store=store,
        scope=EffectScope("database"),
    )
    real.provide("db.sessions", "REAL")

    hijack_scope = EffectScope("hijacker")
    hijacker = Context(
        owner="hijacker",
        requires=frozenset(),
        provides=frozenset({"other"}),
        store=store,
        scope=hijack_scope,
    )
    with pytest.raises(UndeclaredProvide):
        hijacker.provide("db.sessions", "FAKE")

    reader = Context(
        owner="r", requires=frozenset({"db.sessions"}), store=store, scope=EffectScope("r")
    )
    assert reader.get("db.sessions") == "REAL"


def test_isolate_keeps_the_right_to_publish() -> None:
    """A derived context is the same capability through a different realm. It
    has not earned fewer rights, and losing them would make isolation useless
    for the project-scoping case it exists for."""
    store = Store()
    ctx = Context(
        owner="p",
        requires=frozenset(),
        provides=frozenset({"budget"}),
        store=store,
        scope=EffectScope("p"),
    )
    ctx.isolate("budget", "project-a").provide("budget", "A")
    reader = Context(owner="r", requires=frozenset({"budget"}), store=store, scope=EffectScope("r"))
    assert reader.isolate("budget", "project-a").get("budget") == "A"


# -- duplicate providers ----------------------------------------------------


def test_two_providers_of_one_key_are_refused() -> None:
    specs = {
        "core": _spec("core", provides=frozenset({"db.sessions"})),
        "shadow": _spec("shadow", provides=frozenset({"db.sessions"})),
    }
    with pytest.raises(AmbiguousProvider, match=r"db\.sessions"):
        topological_order(specs)


def test_the_refusal_names_both_providers() -> None:
    """So the operator can tell which plugin shadowed which service."""
    specs = {
        "core": _spec("core", provides=frozenset({"k"})),
        "shadow": _spec("shadow", provides=frozenset({"k"})),
    }
    with pytest.raises(AmbiguousProvider) as caught:
        topological_order(specs)
    assert caught.value.providers == ("core", "shadow")
