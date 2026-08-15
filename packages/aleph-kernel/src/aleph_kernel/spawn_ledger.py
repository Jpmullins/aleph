"""The agent family tree, and the brakes on it.

Taken from prime-agent's "supervisor-owned rlm spawn ledger as family authority".
The idea that makes it work is ownership: the ledger belongs to the supervisor,
not to the agents in it. An agent cannot record its own lineage, cannot revise
its parent, and cannot grant itself budget — so the tree is a fact about the
system rather than a claim by a participant.

Aleph has an ActionLedger for row mutations and had no notion of which agent
spawned which, under what budget, to what depth. For a system whose thesis is
that agents improve themselves, "who asked for this" is not bookkeeping: an
agent that can spawn agents can exhaust a budget, recurse forever, or launder an
action through a child that the parent was refused.

Three brakes, all enforced here rather than requested in a prompt:

* **Depth.** A chain longer than `max_depth` is refused.
* **Fan-out.** A parent may not exceed `max_children` direct children.
* **Budget.** A child's budget is deducted from its parent's remaining budget,
  so a subtree can never spend more than its root was given. This is the one
  that actually bounds cost, because depth and fan-out limits still permit a
  wide shallow tree of expensive agents.

In-memory and per-supervisor. Durable lineage belongs in the ActionLedger; this
is the live structure the supervisor enforces against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from aleph_kernel.errors import KernelError

__all__ = ["SpawnDenied", "SpawnLedger", "SpawnRecord", "SpawnState"]


class SpawnState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    KILLED = "killed"


class SpawnDenied(KernelError):
    """A spawn was refused. The reason is always specific enough to act on."""


@dataclass
class SpawnRecord:
    id: UUID
    parent_id: UUID | None
    purpose: str
    depth: int
    budget: float
    spent: float = 0.0
    state: SpawnState = SpawnState.RUNNING
    children: list[UUID] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.budget - self.spent)


class SpawnLedger:
    """Owned by the supervisor. Agents are recorded in it, never authors of it."""

    def __init__(self, *, max_depth: int = 4, max_children: int = 8) -> None:
        self._records: dict[UUID, SpawnRecord] = {}
        self._max_depth = max_depth
        self._max_children = max_children

    # -- reads ---------------------------------------------------------------

    def get(self, spawn_id: UUID) -> SpawnRecord:
        if spawn_id not in self._records:
            msg = f"unknown spawn {spawn_id}"
            raise SpawnDenied(msg)
        return self._records[spawn_id]

    def lineage(self, spawn_id: UUID) -> list[UUID]:
        """Root-first path to ``spawn_id``. The answer to "who asked for this"."""
        path: list[UUID] = []
        current: UUID | None = spawn_id
        while current is not None:
            path.append(current)
            current = self._records[current].parent_id
        return list(reversed(path))

    def descendants(self, spawn_id: UUID) -> list[UUID]:
        """Every spawn beneath this one, breadth-first."""
        out: list[UUID] = []
        queue = list(self._records[spawn_id].children)
        while queue:
            current = queue.pop(0)
            out.append(current)
            queue.extend(self._records[current].children)
        return out

    def subtree_spent(self, spawn_id: UUID) -> float:
        """What this spawn and everything under it has spent."""
        return self._records[spawn_id].spent + sum(
            self._records[d].spent for d in self.descendants(spawn_id)
        )

    def live(self) -> list[SpawnRecord]:
        return [r for r in self._records.values() if r.state is SpawnState.RUNNING]

    # -- writes --------------------------------------------------------------

    def open_root(self, purpose: str, *, budget: float) -> SpawnRecord:
        """Start a root agent. Only the supervisor calls this."""
        record = SpawnRecord(id=uuid4(), parent_id=None, purpose=purpose, depth=0, budget=budget)
        self._records[record.id] = record
        return record

    def spawn(self, parent_id: UUID, purpose: str, *, budget: float) -> SpawnRecord:
        """Record a child, or refuse with a reason.

        The budget is deducted from the parent's remaining allowance rather than
        added alongside it, so a subtree cannot outspend its root no matter how
        it is shaped. Depth and fan-out limits alone would still permit a wide
        shallow tree of expensive agents.
        """
        parent = self.get(parent_id)

        if parent.state is not SpawnState.RUNNING:
            msg = f"parent {parent_id} is {parent.state.value}; it cannot spawn"
            raise SpawnDenied(msg)
        if parent.depth + 1 > self._max_depth:
            msg = (
                f"depth {parent.depth + 1} exceeds the limit of {self._max_depth}; "
                f"lineage {[str(i)[:8] for i in self.lineage(parent_id)]}"
            )
            raise SpawnDenied(msg)
        if len(parent.children) >= self._max_children:
            msg = (
                f"parent {parent_id} already has {len(parent.children)} children "
                f"(limit {self._max_children})"
            )
            raise SpawnDenied(msg)
        if budget <= 0:
            msg = "a child needs a positive budget; a zero-budget agent cannot do anything"
            raise SpawnDenied(msg)
        if budget > parent.remaining:
            msg = (
                f"child budget {budget} exceeds the parent's remaining {parent.remaining}; "
                f"a subtree cannot outspend its root"
            )
            raise SpawnDenied(msg)

        # Reserve immediately. Deferring until the child spends would let a
        # parent promise the same budget to several children at once.
        parent.spent += budget
        child = SpawnRecord(
            id=uuid4(),
            parent_id=parent_id,
            purpose=purpose,
            depth=parent.depth + 1,
            budget=budget,
        )
        self._records[child.id] = child
        parent.children.append(child.id)
        return child

    def record_spend(self, spawn_id: UUID, amount: float) -> None:
        """Charge an agent. Overspending is refused, not merely reported."""
        record = self.get(spawn_id)
        if amount < 0:
            msg = "spend cannot be negative"
            raise SpawnDenied(msg)
        if record.spent + amount > record.budget:
            msg = (
                f"spawn {spawn_id} would spend {record.spent + amount} against a "
                f"budget of {record.budget}"
            )
            raise SpawnDenied(msg)
        record.spent += amount

    def close(self, spawn_id: UUID, *, state: SpawnState = SpawnState.FINISHED) -> None:
        """Close a spawn and everything under it.

        Closing a parent closes its descendants: a child whose parent is gone
        has nobody to return to, and leaving it RUNNING is how an orphan keeps
        spending against a budget nobody is watching.
        """
        for descendant in self.descendants(spawn_id):
            record = self._records[descendant]
            if record.state is SpawnState.RUNNING:
                record.state = SpawnState.KILLED
                record.finished_at = datetime.now(UTC)
        record = self.get(spawn_id)
        record.state = state
        record.finished_at = datetime.now(UTC)
