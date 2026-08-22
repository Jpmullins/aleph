"""A session maker that keeps the cost rows in memory instead of a database.

`LiteLLMClient` writes a `ModelCall` + `CostLedgerEvent` in one transaction on
every call, so it cannot be constructed without a session maker. That single
constructor argument is why every unit test near the model path stubbed the
*client* instead — a hand-written object with an `.embed()` method — and a stub
client proves nothing about the thing that bills anybody. The embed-dimension
guard was asserted against exactly such a stub: "the method was not called",
which is a weaker claim than "no request reached the gateway and no cost row
was written".

`CostWriter.record_call` only ever calls `session.add()` and `session.flush()`,
so an in-memory stand-in is honest here rather than a re-implementation: the
same ORM objects are constructed by the same code, they are just never sent
anywhere. Anything that needs a real transaction — the ledger hash chain,
`project_id` scoping, rollback behaviour — belongs in an integration test
against Postgres and must not be written against this.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aleph_db.models.cost import CostLedgerEvent, ModelCall

__all__ = ["RecordingSessions"]


class _RecordingSession:
    """The subset of `AsyncSession` that `CostWriter` actually touches.

    Crucially, it models the ONE behaviour that makes a session a session: an
    object that was added and never committed does not exist. This double
    originally appended straight to a shared list, so deleting the
    `await session.commit()` that persists every `ModelCall` left the entire
    suite green — including the test whose docstring said it proved the fake was
    "rich enough to drive the real cost path". A double that cannot tell a
    written row from an unwritten one cannot be the basis of a workstream about
    making spend trustworthy.
    """

    def __init__(self, owner: RecordingSessions) -> None:
        self._owner = owner
        self._pending: list[Any] = []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        # Anything still pending at close was never committed. Dropped, exactly
        # as a real session drops it.
        self._pending.clear()
        return False

    def add(self, obj: Any) -> None:
        self._pending.append(obj)
        # Kept for tests that want to see the attempt as well as the outcome —
        # "was a row constructed" and "was a row written" are different
        # questions, and the second one is the one `model_calls()` answers.
        self._owner.attempted.append(obj)

    async def flush(self) -> None:
        self._owner.flushes += 1

    async def commit(self) -> None:
        self._owner.commits += 1
        self._owner.added.extend(self._pending)
        self._pending.clear()

    async def rollback(self) -> None:
        self._owner.rollbacks += 1
        self._pending.clear()

    async def close(self) -> None:  # pragma: no cover - completeness
        self._pending.clear()


class RecordingSessions:
    """Callable stand-in for `async_sessionmaker`, keeping what was added.

    Pass it straight to `LiteLLMClient(session_maker=...)`. Each call returns a
    fresh session that appends to the same shared lists, so `model_calls()`
    spans the whole test rather than the last call only.
    """

    def __init__(self) -> None:
        #: Objects that were added AND committed. This is what `model_calls()`
        #: and `ledger_events()` read, because it is what a real database would
        #: hold.
        self.added: list[Any] = []
        #: Objects handed to `add()`, committed or not. For the rare test that
        #: wants to assert a row was constructed and then rolled back.
        self.attempted: list[Any] = []
        self.commits = 0
        self.flushes = 0
        self.rollbacks = 0

    def __call__(self) -> _RecordingSession:
        return _RecordingSession(self)

    def model_calls(self) -> list[ModelCall]:
        """Every `ModelCall` the client recorded, in order.

        Imported lazily for the same reason `LiteLLMClient` does it: importing
        `aleph_db` at module scope drags the ORM into processes that only ever
        wanted the transport.
        """
        from aleph_db.models.cost import ModelCall as _ModelCall

        return [o for o in self.added if isinstance(o, _ModelCall)]

    def ledger_events(self) -> list[CostLedgerEvent]:
        from aleph_db.models.cost import CostLedgerEvent as _CostLedgerEvent

        return [o for o in self.added if isinstance(o, _CostLedgerEvent)]
