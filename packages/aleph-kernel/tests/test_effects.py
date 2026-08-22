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


# ---------------------------------------------------------------------------
# The two properties the module docstring says are "not in the stdlib and are
# the reason this module exists" — and which nothing exercised
# ---------------------------------------------------------------------------
#
# Found by mutation: deleting `drive`'s pre-step guard, and deleting the branch
# that runs a straggler inverse, both left all 153 kernel tests green. The LIFO
# and at-most-once properties above were covered; the guarded-iteration half of
# Algorithm 1 was not, so `contextlib.AsyncExitStack` would have passed this
# suite and the module's whole justification was untested.


async def test_disposal_stops_the_setup_at_the_next_step_boundary() -> None:
    """The guard is consulted BEFORE every step, not only after one.

    Without the pre-step check, a setup that is unwound mid-flight runs one more
    step first — it opens the next resource and only then discovers the scope is
    gone. On a real capability that step is a connection, a file handle or a
    row: work performed against a system that has already been told to stop.

    Driven through a setup that RECORDS each step, so the assertion is about
    what ran rather than about what the scope thinks it holds.
    """
    steps: list[str] = []
    scope = EffectScope("t")

    async def setup() -> AsyncIterator[Callable[[], Awaitable[None]]]:
        steps.append("step-1")
        yield _recorder([], "undo-1")
        # Everything past here must not run: disposal began during step 1.
        steps.append("step-2")
        yield _recorder([], "undo-2")
        steps.append("step-3")
        yield _recorder([], "undo-3")

    agen = setup()
    # Take the first step by hand, then dispose, then hand the rest to `drive`.
    # This is the real interleaving — disposal arriving while a setup is part
    # way through — reproduced without a second task, so there is no race for
    # the test itself to lose.
    first = await anext(agen)
    scope.push(first)
    await scope.unwind()
    await scope.drive(agen)

    assert steps == ["step-1"], (
        f"the setup ran {steps} after its scope was unwound; the guard is "
        "checked before every step precisely so it does not"
    )


async def test_an_inverse_that_arrives_after_disposal_is_run_not_dropped() -> None:
    """The leaky edge of Algorithm 1, and the module says so in a comment.

    A step that completes *while* disposal is running yields an inverse into a
    queue that has already been drained. Nothing will unwind it later, so it
    has to be run on the spot — dropping it strands whatever it holds, and the
    resource leak is invisible because every counter says the scope is empty.

    `unwind` is called from inside the setup body, which is exactly the shape
    that produces the straggler: the effect is performed, then the queue is
    gone, then the inverse is yielded.
    """
    log: list[str] = []
    scope = EffectScope("t")

    async def setup() -> AsyncIterator[Callable[[], Awaitable[None]]]:
        log.append("opened-straggler")
        await scope.unwind()  # disposal begins mid-step
        yield _recorder(log, "closed-straggler")
        log.append("never")  # pragma: no cover - drive returns before this

    await scope.drive(setup())

    assert log == ["opened-straggler", "closed-straggler"], (
        "the inverse yielded after the queue was drained was discarded; "
        "whatever that step opened is now unreachable and unclosed"
    )
    assert scope.depth == 0, "a straggler was pushed onto a scope nothing will unwind"
