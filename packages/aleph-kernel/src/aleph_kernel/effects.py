"""Revertible effects — paper §5.1.1, Algorithm 1.

Every context mutation flows through one primitive, so every mutation is
tracked and every tracked mutation can be undone. That is the whole of temporal
composability, and it is why a capability can be removed at runtime without
leaving residue.

`contextlib.AsyncExitStack` gives LIFO disposal and nothing else. Three
properties here are not in the stdlib and are the reason this module exists:

* **Guard-checked iteration.** The guard is consulted before every step, so a
  setup interrupted mid-way rolls back exactly the inverses it had accumulated
  — partial rollback at iteration boundaries, not all-or-nothing.
* **Armed at-most-once.** Recovery fires once. Applying an inverse at a state no
  application of the effect produced reverts nothing, so a second unwind is a
  corruption, not a no-op.
* **Failure still unwinds.** A setup that raises halfway has already changed the
  world; the inverses it yielded before raising must run.

cordis honours the guard only on its async-iterator path (`fiber.ts:263`); its
sync branch loops `while (true)` with no guard at all. Normalising to one
guarded path is a deliberate improvement, not an accident of the port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from aleph_kernel.tracing import start_span

if TYPE_CHECKING:
    from types import TracebackType

__all__ = ["EffectScope", "Inverse"]

Inverse = Callable[[], Awaitable[None]]


class EffectScope:
    """Accumulates the inverses of one capability's effects and unwinds them LIFO.

    One scope per capability instance. `unwind` is idempotent by arming, and
    aggregates failures rather than stopping at the first: a later inverse must
    still run when an earlier one raises, or a single bad teardown strands every
    resource beneath it.
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._inverses: list[Inverse] = []
        self._armed = True

    @property
    def armed(self) -> bool:
        """False once unwound. Doubles as Algorithm 1's iteration guard."""
        return self._armed

    @property
    def depth(self) -> int:
        return len(self._inverses)

    def push(self, inverse: Inverse) -> None:
        """Record an inverse. Prepending on unwind is what yields LIFO recovery."""
        if not self._armed:
            msg = f"cannot register an effect on {self._owner!r} after it has been unwound"
            raise RuntimeError(msg)
        self._inverses.append(inverse)

    async def drive(self, agen: AsyncIterator[Inverse]) -> None:
        """Run a setup generator, collecting each yielded inverse (Algorithm 1).

        The guard is checked before every step. If the generator raises, the
        inverses collected so far are kept — the caller unwinds them — because
        a setup that failed at step 4 has already performed steps 1 through 3.
        """
        while True:
            if not self._armed:
                await agen.aclose()
                return
            try:
                inverse = await anext(agen)
            except StopAsyncIteration:
                return
            if self._armed:
                self.push(inverse)
                continue
            # Disposal began *during* that step, so this inverse arrived after
            # the queue was already drained and nothing will unwind it later.
            # The effect happened; undo it now rather than dropping it. This is
            # the leaky edge of Algorithm 1's step-boundary interruption, and
            # discarding the straggler would strand whatever it holds.
            await inverse()
            await agen.aclose()
            return

    async def unwind(self) -> None:
        """Run every inverse in LIFO order, exactly once, even if some raise."""
        if not self._armed:
            return
        self._armed = False
        failures: list[BaseException] = []
        with start_span("kernel.effect.unwind", **{"aleph.capability": self._owner}) as span:
            span.set_attribute("aleph.kernel.inverses", len(self._inverses))
            while self._inverses:
                inverse = self._inverses.pop()
                try:
                    await inverse()
                except Exception as exc:  # every inverse must get its turn
                    failures.append(exc)
        if failures:
            msg = f"{len(failures)} inverse(s) failed while unwinding {self._owner!r}"
            raise ExceptionGroup(msg, failures)

    async def __aenter__(self) -> EffectScope:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        await self.unwind()
        return False
