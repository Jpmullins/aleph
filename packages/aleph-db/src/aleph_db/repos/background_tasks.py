"""A ticket for work that outlives the request that asked for it.

Measured on the deployed stack (`scripts/_acceptance/agent_turn_probe.py`), a
tool-using chat turn shows the analyst **nothing for 16 to 20 seconds**: the
assistant calls a tool, waits on it inline, and no text streams until every tool
has returned. There is exactly one escape hatch today — ``start_research``,
which POSTs ``/synthesize`` and returns straight away — and it is hardcoded for
one kind of work. This module is the general form of that escape hatch: create a
run row, hand its id back as a ticket, and let a worker do the slow part.

**Why this reuses ``agent_runs`` rather than adding a ``background_jobs`` table.**
A parallel job table would be a second, competing record of "what the system is
doing", and the Inspector pane (``InspectorSurface.tsx``) reads ``agent_runs`` +
``agent_events``. A ticket the assistant hands back has to be visible *there*,
in the same timeline as the turn that created it, or the analyst has two places
to look and neither is complete. The dispatch also writes an ``AgentEvent``
against the **parent** chat run, so the conversation's own timeline says "I
started this" rather than the ticket existing only in a place nobody opened.

**Cancellation is cooperative, and the two writers never share a column.**
The API marks a cancellation by writing ``input_payload[CANCEL_REQUESTED_KEY]``;
the worker proves it is alive by writing ``result_payload[HEARTBEAT_KEY]``.
Those are deliberately *different JSONB columns*. Both sides read-modify-write a
whole JSON document, so if they shared one column a heartbeat committed
milliseconds after a cancel request would overwrite the request with a stale
copy — the cancel would vanish, silently, and only under load. Splitting them
makes that lost update unexpressible rather than unlikely.

Note the reassignment in every writer below: SQLAlchemy does not track in-place
mutation of a plain ``JSONB`` dict, so ``run.input_payload["x"] = 1`` commits
nothing at all. That failure is completely silent — the flush succeeds, the
UPDATE simply has no SET for that column — which is exactly the shape of defect
this repository keeps finding.

**The reaper.** ``aleph_db.repos.agent_runs.reap_stale_runs`` fails any run
sitting in ``running`` for longer than an hour, on the assumption that the
process which owned it died. A background task is precisely the kind of run that
can legitimately outlive that deadline, so the heartbeat written here is what
keeps a live task from being reaped out from under itself; see
``stale_running_runs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.errors import ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentEvent, AgentRun
from aleph_db.repos.ledger import LedgerWriter

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

#: The kinds of work a ticket may name.
#:
#: This list lives in the DB layer and not next to the worker handlers because
#: **apps never import apps**: the API route validates a requested kind, the
#: worker binds a handler to it, and neither may import the other. The two are
#: held in agreement by
#: ``apps/workers/tests/test_background_task_kinds.py::test_every_kind_has_a_handler``,
#: which fails in both directions — an accepted kind with no handler is a ticket
#: that can only ever fail, and a handler nobody can request is dead code.
BACKGROUND_TASK_KINDS: tuple[str, ...] = (
    "reindex_corpus",
    "review_sweep",
    # Hard-delete the rows behind soft-deleted projects. Without it a
    # deployment keeps every project it has ever held: measured at 1.6M rows
    # behind 1,135 dead projects on one instance. See `aleph_db.purge`.
    "purge_deleted_projects",
)

#: Where the chat turn that asked for the work is recorded on the child run.
PARENT_RUN_KEY = "parent_agent_run_id"

#: Written by the API into ``input_payload`` when someone asks to stop the work.
CANCEL_REQUESTED_KEY = "cancel_requested_at"

#: Written by the worker into ``result_payload`` at every checkpoint. Read by
#: the stale-run reaper, which must not kill a task that is demonstrably alive.
HEARTBEAT_KEY = "heartbeat_at"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
#: 9 characters. ``agent_runs.status`` is ``String(16)``; anything longer is
#: truncated by Postgres on some paths and raises on others.
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED})

#: Emitted against the PARENT run so the conversation's own Inspector timeline
#: carries the hand-off, rather than the ticket living only in a pane nobody
#: opened.
DISPATCH_EVENT_KIND = "background_task_dispatched"

ACTION_DISPATCH = "background_task.dispatch"
ACTION_CANCEL_REQUESTED = "background_task.cancel_requested"
ACTION_CANCELLED = "background_task.cancelled"
ACTION_SUCCEEDED = "background_task.succeeded"
ACTION_FAILED = "background_task.failed"


@dataclass(frozen=True)
class Ticket:
    """What the caller gets back instead of waiting."""

    run_id: UUID
    project_id: UUID
    kind: str
    correlation_id: str
    status: str


@dataclass(frozen=True)
class TicketView:
    """Everything a "how is it going?" answer can be built from."""

    run_id: UUID
    project_id: UUID
    kind: str
    status: str
    terminal: bool
    cancel_requested: bool
    phase: str | None
    phase_event_kind: str | None
    phase_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    parent_agent_run_id: UUID | None
    params: dict[str, Any]
    result: dict[str, Any] | None
    error_text: str | None


@dataclass(frozen=True)
class CancelOutcome:
    """What asking to stop actually did.

    ``outcome`` is not cosmetic. "I marked it cancelled" and "I asked it to
    stop" are different promises, and an assistant that reports the first when
    only the second happened is lying about work that is still running.
    """

    run_id: UUID
    status: str
    outcome: str  # "cancelled" | "cancel_requested" | "already_terminal"


@dataclass(frozen=True)
class Checkpoint:
    """The worker's answer to "may I keep going?"."""

    cancel_requested: bool
    missing: bool


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _parse_ts(raw: Any) -> datetime | None:
    """An ISO timestamp out of JSONB, normalised to aware UTC.

    Naive values are read as UTC rather than rejected: everything Aleph writes
    is aware, but a hand-repaired row must not make the reaper raise.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def heartbeat_at(run: AgentRun) -> datetime | None:
    """When this run last proved it was alive, or ``None`` if it never has."""
    return _parse_ts(_as_dict(run.result_payload).get(HEARTBEAT_KEY))


def cancel_requested_at(run: AgentRun) -> datetime | None:
    return _parse_ts(_as_dict(run.input_payload).get(CANCEL_REQUESTED_KEY))


async def create_ticket(
    session: AsyncSession,
    ledger: LedgerWriter,
    *,
    project_id: UUID,
    kind: str,
    params: dict[str, Any],
    actor_id: UUID,
    actor_kind: str,
    parent_agent_run_id: UUID | None = None,
) -> Ticket:
    """Create the ``pending`` run, ledger the dispatch, link it to its parent.

    Does **not** validate ``kind``: the vocabulary is enforced at the HTTP
    boundary, where an unknown kind can be answered with a 422 naming the kinds
    that do exist. A repository that also enforced it would make the test for
    the cancellation machinery impossible to write against a handler of its own,
    and would put the same rule in two places.

    Does **not** commit. The caller commits, and only then enqueues — the worker
    must never race an uncommitted row (a not-found in the job is an exception
    arq retries, and with the row absent it can never be marked failed, so the
    ticket strands as ``pending`` forever).
    """
    run_id = uuid7()
    # Full hex, not a prefix: uuid7's leading bits are a millisecond timestamp,
    # so a truncated id collides for tickets created in the same window against
    # uq_agent_runs_correlation_id.
    correlation_id = f"bg-{run_id.hex}"
    input_payload: dict[str, Any] = {
        "kind": kind,
        "params": params,
        PARENT_RUN_KEY: str(parent_agent_run_id) if parent_agent_run_id is not None else None,
    }
    session.add(
        AgentRun(
            id=run_id,
            project_id=project_id,
            agent_kind=kind,
            correlation_id=correlation_id,
            status=STATUS_PENDING,
            input_payload=input_payload,
            created_by=actor_id,
        )
    )
    await session.flush()

    if parent_agent_run_id is not None:
        # The parent must be in THIS project. `agent_events` carries no
        # `project_id` and no foreign key on `agent_run_id`, so without this
        # check the row below is written against whatever UUID the request body
        # names — and a caller with EDITOR on project A could post a run id
        # owned by project B and have a `background_task_dispatched` row appear
        # in B's timeline, naming A's child run. That was reachable from a
        # prompt: the module docstring applies exactly this reasoning to reads
        # and stopped short of the write.
        #
        # The error does not distinguish "no such run" from "a run in another
        # project", so it cannot be used to probe for run ids either.
        parent_project = (
            await session.execute(
                select(AgentRun.project_id).where(AgentRun.id == parent_agent_run_id)
            )
        ).scalar_one_or_none()
        if parent_project != project_id:
            msg = "parent_agent_run_id does not name an agent run in this project"
            raise ValidationFailed(msg)

        # The hand-off, written where the conversation is already being read.
        # Without this the ticket exists but the turn that created it shows a
        # gap, and "the assistant said it started something" is unverifiable
        # from the record.
        session.add(
            AgentEvent(
                id=uuid7(),
                agent_run_id=parent_agent_run_id,
                event_kind=DISPATCH_EVENT_KIND,
                payload_jsonb={
                    "phase": kind,
                    "child_agent_run_id": str(run_id),
                    "kind": kind,
                },
            )
        )

    await ledger.append(
        project_id=project_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        action_kind=ACTION_DISPATCH,
        target_id=run_id,
        target_kind="agent_run",
        payload={
            "kind": kind,
            "params": params,
            PARENT_RUN_KEY: str(parent_agent_run_id) if parent_agent_run_id else None,
        },
        trace_id=None,
    )
    return Ticket(
        run_id=run_id,
        project_id=project_id,
        kind=kind,
        correlation_id=correlation_id,
        status=STATUS_PENDING,
    )


async def read_ticket(
    session: AsyncSession, *, project_id: UUID, run_id: UUID
) -> TicketView | None:
    """Status plus the most recent phase event, or ``None`` if it is not this
    project's ticket.

    Scoped by ``project_id`` in the WHERE clause, not filtered afterwards: a
    ticket id is a bare UUID an agent could be told by anything, and a read that
    resolved it across projects would be a cross-project leak reachable from a
    prompt.
    """
    run = (
        await session.execute(
            select(AgentRun).where(AgentRun.id == run_id, AgentRun.project_id == project_id)
        )
    ).scalar_one_or_none()
    if run is None:
        return None

    latest = (
        await session.execute(
            select(AgentEvent)
            .where(AgentEvent.agent_run_id == run_id)
            .order_by(AgentEvent.timestamp.desc(), AgentEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    payload = _as_dict(run.input_payload)
    raw_parent = payload.get(PARENT_RUN_KEY)
    parent: UUID | None = None
    if isinstance(raw_parent, str):
        try:
            parent = UUID(raw_parent)
        except ValueError:
            parent = None

    event_payload = _as_dict(latest.payload_jsonb) if latest is not None else {}
    phase = event_payload.get("phase")
    return TicketView(
        run_id=run.id,
        project_id=run.project_id,
        kind=run.agent_kind,
        status=run.status,
        terminal=run.status in TERMINAL_STATUSES,
        cancel_requested=cancel_requested_at(run) is not None,
        phase=phase if isinstance(phase, str) else None,
        phase_event_kind=latest.event_kind if latest is not None else None,
        phase_at=latest.timestamp if latest is not None else None,
        started_at=run.started_at,
        completed_at=run.completed_at,
        parent_agent_run_id=parent,
        params=_as_dict(payload.get("params")),
        result=dict(run.result_payload) if isinstance(run.result_payload, dict) else None,
        error_text=run.error_text,
    )


async def request_cancel(
    session: AsyncSession,
    ledger: LedgerWriter,
    *,
    project_id: UUID,
    run_id: UUID,
    actor_id: UUID,
    actor_kind: str,
) -> CancelOutcome | None:
    """Stop the work. Returns ``None`` when the ticket is not this project's.

    Two branches, because "not started yet" and "already running" are genuinely
    different situations:

    * ``pending`` — nothing is executing, so this call *is* the cancellation.
      The status change and its ledger row are written here, in one transaction.
      The worker, if the job is already queued, sees a terminal row and no-ops.
    * ``running`` — a process owns this work and only it can stop cleanly. The
      request is recorded and ledgered; the worker converges the status at its
      next checkpoint.
    """
    run = (
        await session.execute(
            select(AgentRun).where(AgentRun.id == run_id, AgentRun.project_id == project_id)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    if run.status in TERMINAL_STATUSES:
        return CancelOutcome(run_id=run_id, status=run.status, outcome="already_terminal")

    now = utcnow()
    # Reassign, never mutate in place — see the module docstring. An in-place
    # write here would produce an UPDATE with no SET for input_payload, the
    # request would be lost, and every symptom would point at the worker.
    run.input_payload = {**_as_dict(run.input_payload), CANCEL_REQUESTED_KEY: now.isoformat()}

    if run.status == STATUS_PENDING:
        run.status = STATUS_CANCELLED
        run.completed_at = now
        run.error_text = "cancelled before the worker started it"
        await ledger.append(
            project_id=project_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            action_kind=ACTION_CANCELLED,
            target_id=run_id,
            target_kind="agent_run",
            payload={"kind": run.agent_kind, "was": STATUS_PENDING},
            trace_id=None,
        )
        return CancelOutcome(run_id=run_id, status=STATUS_CANCELLED, outcome="cancelled")

    await ledger.append(
        project_id=project_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        action_kind=ACTION_CANCEL_REQUESTED,
        target_id=run_id,
        target_kind="agent_run",
        payload={"kind": run.agent_kind, "was": run.status},
        trace_id=None,
    )
    return CancelOutcome(run_id=run_id, status=run.status, outcome="cancel_requested")


async def claim_ticket(
    session: AsyncSession, *, run_id: UUID
) -> tuple[AgentRun | None, str | None]:
    """Move a ``pending`` ticket to ``running``. Returns ``(run, refusal)``.

    ``refusal`` is a plain-language reason the job should stop before doing any
    work, and it is not an error condition:

    * the row is gone — nothing to converge;
    * the row is already terminal — arq re-delivered a job whose worker died
      after the final commit but before acking, or the ticket was cancelled
      while queued. Flipping a succeeded run to anything else here is the
      specific way an idempotent retry destroys a good result;
    * the row is already ``running`` — a prior attempt was interrupted and arq
      re-enqueued it. Re-running would duplicate the fan-out, so the caller
      converges it to ``failed`` instead. Same reasoning as
      ``deep_research_job``.

    Does not commit; the caller owns the transaction.
    """
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        return None, f"agent run {run_id} not found"
    if run.status in TERMINAL_STATUSES:
        return run, f"already terminal ({run.status})"
    if run.status == STATUS_RUNNING:
        return run, "interrupted by a prior attempt; not retried"
    run.status = STATUS_RUNNING
    run.started_at = utcnow()
    # First heartbeat at the moment of claiming: without it a task whose first
    # unit of work takes longer than the reaper's deadline is reaped while it is
    # still in that first unit.
    run.result_payload = {**_as_dict(run.result_payload), HEARTBEAT_KEY: utcnow().isoformat()}
    return run, None


async def checkpoint(
    maker: Callable[[], Any], *, run_id: UUID, heartbeat: bool = True
) -> Checkpoint:
    """ "May I keep going?" — one round trip, in its own short-lived session.

    Its own session on purpose: the worker's data transaction may be long, and a
    cancellation written by the API after that transaction opened would be
    invisible inside it. Reading the flag in a fresh session is the difference
    between a cancel that lands in a second and one that lands when the job
    happens to commit.

    The heartbeat rides along on the same round trip rather than being a second
    write, because a liveness signal that costs an extra query per unit of work
    is a liveness signal somebody will remove.
    """
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return Checkpoint(cancel_requested=False, missing=True)
        requested = cancel_requested_at(run) is not None
        if heartbeat:
            run.result_payload = {
                **_as_dict(run.result_payload),
                HEARTBEAT_KEY: utcnow().isoformat(),
            }
            await session.commit()
        return Checkpoint(cancel_requested=requested, missing=False)


async def finish_ticket(
    session: AsyncSession,
    ledger: LedgerWriter,
    *,
    run_id: UUID,
    status: str,
    actor_id: UUID,
    actor_kind: str,
    result: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> bool:
    """Move a ticket to a terminal status and ledger it in the same transaction.

    One function for all three terminal statuses so that no exit path can be the
    one that forgets the ledger row. ``cancelled`` in particular has to be
    auditable — "who stopped this, and when" is the whole reason a cancel is a
    state change rather than a killed process.
    """
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        return False
    run.status = status
    run.completed_at = utcnow()
    if error_text is not None:
        run.error_text = error_text[:4096]
    # The heartbeat is deliberately preserved alongside the result: "last seen
    # alive at" is evidence when a task is argued about afterwards.
    run.result_payload = {**_as_dict(run.result_payload), **(result or {})}
    action = {
        STATUS_CANCELLED: ACTION_CANCELLED,
        STATUS_SUCCEEDED: ACTION_SUCCEEDED,
        STATUS_FAILED: ACTION_FAILED,
    }.get(status, ACTION_FAILED)
    await ledger.append(
        project_id=run.project_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        action_kind=action,
        target_id=run_id,
        target_kind="agent_run",
        payload={"kind": run.agent_kind, "status": status, "result": result or {}},
        trace_id=None,
    )
    return True


__all__ = [
    "ACTION_CANCELLED",
    "ACTION_CANCEL_REQUESTED",
    "ACTION_DISPATCH",
    "ACTION_FAILED",
    "ACTION_SUCCEEDED",
    "BACKGROUND_TASK_KINDS",
    "CANCEL_REQUESTED_KEY",
    "DISPATCH_EVENT_KIND",
    "HEARTBEAT_KEY",
    "PARENT_RUN_KEY",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "TERMINAL_STATUSES",
    "CancelOutcome",
    "Checkpoint",
    "Ticket",
    "TicketView",
    "cancel_requested_at",
    "checkpoint",
    "claim_ticket",
    "create_ticket",
    "finish_ticket",
    "heartbeat_at",
    "read_ticket",
    "request_cancel",
]
