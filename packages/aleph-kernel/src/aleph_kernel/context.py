"""The context — paper §5.1.2 (coeffect operations) and §5.1.4 (mediated access).

A context is a capability's whole view of the world. It resolves only what that
capability declared, which is what turns a declaration from documentation into
an enforced boundary: reaching for something undeclared raises rather than
returning it.

The paper suggests the descriptor protocol for the mediation; ``__getattr__`` is
the right Python mechanism instead. Descriptors install on the *class*, so a
dynamic key set would mean mutating a shared class object at runtime, whereas
``__getattr__`` fires only on lookup miss — exactly the coeffect case, and
per-instance.

Two-layer resolution (Algorithm 2): ``key -> realm -> value``. The realm
indirection is what makes ``isolate`` possible, and ``isolate`` is how a project
gets its own binding of a shared service name. Aleph currently spells
``project_id`` by hand in ~1,500 places and relies on every author remembering;
a realm makes tenant scoping a property of the context a capability runs in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aleph_kernel.errors import InactiveAccess, UndeclaredAccess

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aleph_kernel.effects import EffectScope

__all__ = ["Context", "Store"]

#: The realm every binding lands in unless a context isolates the key.
ROOT_REALM = "root"


class Store:
    """The coeffect store: values keyed by (realm, key), plus the realm map."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], Any] = {}
        #: Which capability provided each binding. A withdrawal is visible to
        #: dependents because this empties before their teardown runs.
        self._owners: dict[tuple[str, str], str] = {}

    def put(self, realm: str, key: str, value: Any, owner: str) -> None:
        self._values[(realm, key)] = value
        self._owners[(realm, key)] = owner

    def drop(self, realm: str, key: str) -> None:
        self._values.pop((realm, key), None)
        self._owners.pop((realm, key), None)

    def get(self, realm: str, key: str) -> tuple[bool, Any]:
        """(found, value). Falls back to the root realm so isolation is additive."""
        if (realm, key) in self._values:
            return True, self._values[(realm, key)]
        if realm != ROOT_REALM and (ROOT_REALM, key) in self._values:
            return True, self._values[(ROOT_REALM, key)]
        return False, None

    def owner_of(self, realm: str, key: str) -> str | None:
        return self._owners.get((realm, key)) or self._owners.get((ROOT_REALM, key))


class Context:
    """A capability's scoped view. Resolves declared keys and nothing else."""

    def __init__(
        self,
        *,
        owner: str,
        requires: frozenset[str],
        store: Store,
        scope: EffectScope,
        realms: dict[str, str] | None = None,
    ) -> None:
        # Every attribute set here must be assigned before __getattr__ can fire,
        # and every one is private so it cannot collide with a service name.
        self._owner = owner
        self._requires = requires
        self._store = store
        self._scope = scope
        self._realms: dict[str, str] = dict(realms or {})

    # -- reads ---------------------------------------------------------------

    def __getattr__(self, key: str) -> Any:
        """Resolve a declared service as an attribute: ``ctx.db``.

        Only called on lookup miss, so real attributes never reach here. Private
        and dunder names are re-raised as ``AttributeError`` so copy, pickle and
        the debugger behave normally instead of being told they undeclared a
        capability.
        """
        if key.startswith("_"):
            raise AttributeError(key)
        return self.get(key)

    def get(self, key: str) -> Any:
        """Explicit resolution. Same rules as attribute access.

        Algorithm 6: undeclared is refused outright; declared-but-unprovided is
        a distinct error, because the capability did nothing wrong — its
        dependency is simply not up.
        """
        if key not in self._requires:
            raise UndeclaredAccess(self._owner, key)
        realm = self._realms.get(key, ROOT_REALM)
        found, value = self._store.get(realm, key)
        if not found:
            raise InactiveAccess(self._owner, key)
        return value

    def has(self, key: str) -> bool:
        """True when ``key`` is declared AND currently provided. Never raises."""
        if key not in self._requires:
            return False
        realm = self._realms.get(key, ROOT_REALM)
        found, _ = self._store.get(realm, key)
        return found

    # -- writes --------------------------------------------------------------

    def provide(self, key: str, value: Any) -> None:
        """Publish a service and register its withdrawal as an inverse.

        The inverse is pushed on the providing capability's own scope, so a
        binding disappears exactly when its provider is torn down. There is no
        way to publish without also registering the removal — that is the point.
        """
        realm = self._realms.get(key, ROOT_REALM)
        self._store.put(realm, key, value, self._owner)

        async def _withdraw() -> None:
            self._store.drop(realm, key)

        self._scope.push(_withdraw)

    def effect(self, inverse: Callable[[], Awaitable[None]]) -> None:
        """Register a bare inverse for something changed outside the store."""
        self._scope.push(inverse)

    def isolate(self, key: str, realm: str) -> Context:
        """A derived context in which ``key`` resolves to ``realm``'s binding.

        Recovery is implicit — discarding the child context is the whole undo,
        which is why the paper gives isolation *derived* realization (Definition
        27) and no inverse. Two contexts isolating the same key to different
        realms see independent bindings; that is project scoping.
        """
        return Context(
            owner=self._owner,
            requires=self._requires,
            store=self._store,
            scope=self._scope,
            realms={**self._realms, key: realm},
        )

    def __repr__(self) -> str:
        return f"<Context {self._owner!r} requires={sorted(self._requires)}>"
