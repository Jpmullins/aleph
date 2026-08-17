"""Revertible effects: LIFO, partial rollback, at-most-once, no stranding."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from aleph_kernel.effects import EffectScope

pytestmark = pytest.mark.asyncio


def _recorder(log: list[str], label: str) -> Callable[[], Awaitable[None]]:
    async def inverse() -> None:
        log.append(label)

    return inverse


async def test_inverses_run_in_lifo_order() -> None:
    """Teardown reverses setup: the last thing built is the first thing unbuilt."""
    log: list[str] = []
    scope = EffectScope("t")

    async def setup() -> AsyncIterator[Callable[[], Awaitable[None]]]:
        yield _recorder(log, "undo-a")
        yield _recorder(log, "undo-b")
        yield _recorder(log, "undo-c")

    await scope.drive(setup())
    await scope.unwind()
    assert log == ["undo-c", "undo-b", "undo-a"]


async def test_setup_that_raises_still_unwinds_what_it_built() -> None:
    """THE property. A setup that fails at step 3 has already done steps 1 and 2."""
    log: list[str] = []
    scope = EffectScope("t")

    async def setup() -> AsyncIterator[Callable[[], Awaitable[None]]]:
        yield _recorder(log, "undo-a")
        yield _recorder(log, "undo-b")
        msg = "step 3 blew up"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="step 3"):
        await scope.drive(setup())

    assert scope.depth == 2, "the inverses yielded before the failure were dropped"
    await scope.unwind()
    assert log == ["undo-b", "undo-a"]


async def test_unwind_is_at_most_once() -> None:
    """An inverse applied twice reverts a state no application produced."""
    log: list[str] = []
    scope = EffectScope("t")

    async def setup() -> AsyncIterator[Callable[[], Awaitable[None]]]:
        yield _recorder(log, "undo")

    await scope.drive(setup())
    await scope.unwind()
    await scope.unwind()
    await scope.unwind()
    assert log == ["undo"]


async def test_guard_stops_iteration_and_keeps_what_was_built() -> None:
    """Unwinding mid-setup halts the generator; inverses so far still run."""
    log: list[str] = []
    scope = EffectScope("t")

    async def setup() -> AsyncIterator[Callable[[], Awaitable[None]]]:
        yield _recorder(log, "undo-a")
        await scope.unwind()  # disarms mid-iteration
        log.append("reached-step-2")
        yield _recorder(log, "undo-b")
        log.append("reached-step-3")  # must never run

    await scope.drive(setup())
    assert "undo-a" in log, "the first inverse should have run during unwind"
    assert "reached-step-3" not in log, "iteration continued past a tripped guard"


async def test_a_failing_inverse_does_not_strand_the_rest() -> None:
    """One bad teardown must not leak every resource beneath it."""
    log: list[str] = []
    scope = EffectScope("t")

    async def boom() -> None:
        msg = "close failed"
        raise OSError(msg)

    async def setup() -> AsyncIterator[Callable[[], Awaitable[None]]]:
        yield _recorder(log, "undo-outer")
        yield boom
        yield _recorder(log, "undo-inner")

    await scope.drive(setup())
    with pytest.raises(ExceptionGroup):
        await scope.unwind()
    assert log == ["undo-inner", "undo-outer"], "an inverse was skipped after a sibling raised"


async def test_registering_after_unwind_is_refused() -> None:
    scope = EffectScope("t")
    await scope.unwind()
    with pytest.raises(RuntimeError, match="after it has been unwound"):
        scope.push(_recorder([], "late"))


async def test_context_manager_unwinds_on_exit() -> None:
    log: list[str] = []
    async with EffectScope("t") as scope:
        scope.push(_recorder(log, "undo"))
    assert log == ["undo"]
