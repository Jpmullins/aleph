"""Mediated context access — Algorithm 6, and isolate as project scoping."""

from __future__ import annotations

import pytest

from aleph_kernel.context import Context, Store
from aleph_kernel.effects import EffectScope
from aleph_kernel.errors import InactiveAccess, UndeclaredAccess

pytestmark = pytest.mark.asyncio


def ctx(requires: set[str], store: Store, scope: EffectScope) -> Context:
    return Context(owner="cap", requires=frozenset(requires), store=store, scope=scope)


async def test_declared_and_provided_resolves() -> None:
    store, scope = Store(), EffectScope("p")
    Context(owner="p", requires=frozenset(), store=store, scope=scope).provide("db", "POOL")
    assert ctx({"db"}, store, EffectScope("c")).get("db") == "POOL"


async def test_attribute_access_is_the_same_as_get() -> None:
    store, scope = Store(), EffectScope("p")
    Context(owner="p", requires=frozenset(), store=store, scope=scope).provide("db", "POOL")
    assert ctx({"db"}, store, EffectScope("c")).db == "POOL"


async def test_undeclared_is_refused_even_when_it_exists() -> None:
    """Capability security: existing is not the same as being allowed to have it."""
    store, scope = Store(), EffectScope("p")
    Context(owner="p", requires=frozenset(), store=store, scope=scope).provide("secrets", "S")
    with pytest.raises(UndeclaredAccess, match="without declaring it"):
        ctx({"db"}, store, EffectScope("c")).get("secrets")


async def test_declared_but_unprovided_is_a_distinct_error() -> None:
    """The capability did nothing wrong; its dependency is not up."""
    with pytest.raises(InactiveAccess):
        ctx({"db"}, Store(), EffectScope("c")).get("db")


async def test_has_never_raises() -> None:
    c = ctx({"db"}, Store(), EffectScope("c"))
    assert c.has("db") is False
    assert c.has("undeclared") is False


async def test_private_names_stay_attribute_errors() -> None:
    """copy/pickle/the debugger must not be told they undeclared a capability."""
    c = ctx({"db"}, Store(), EffectScope("c"))
    with pytest.raises(AttributeError):
        _ = c._nonexistent


async def test_provide_registers_its_own_withdrawal() -> None:
    """There is no way to publish a service without also registering its removal."""
    store, scope = Store(), EffectScope("p")
    Context(owner="p", requires=frozenset(), store=store, scope=scope).provide("db", "POOL")
    consumer = ctx({"db"}, store, EffectScope("c"))
    assert consumer.has("db")
    await scope.unwind()
    assert not consumer.has("db"), "the binding outlived its provider"


async def test_isolate_gives_independent_bindings_per_realm() -> None:
    """This is project scoping: same key, different tenant, different value."""
    store = Store()
    root = Context(owner="p", requires=frozenset(), store=store, scope=EffectScope("p"))
    root.provide("budget", "SHARED")

    a = root.isolate("budget", "project-a")
    b = root.isolate("budget", "project-b")
    a.provide("budget", "A-ONLY")

    reader = ctx({"budget"}, store, EffectScope("r"))
    assert reader.isolate("budget", "project-a").get("budget") == "A-ONLY"
    # project-b never bound it, so it falls back to the shared root binding.
    assert reader.isolate("budget", "project-b").get("budget") == "SHARED"
    assert b is not a
