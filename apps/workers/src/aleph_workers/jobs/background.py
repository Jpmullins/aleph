"""``background_task_job`` — one supervisor for every kind of ticketed work.

The assistant hands back a ticket and keeps talking; this is what makes the
ticket true. It owns the four things every long job in this repository has
previously re-implemented (and got wrong at least once each):

* **claiming** — ``pending`` to ``running``, refusing a re-delivered job rather
  than flipping a succeeded run to failed;
* **heartbeating** — proving the run is alive, so the stale-run reaper does not
  mark a working two-hour sweep as dead;
* **cancellation** — a cooperative check between units of work, and a terminal
  ``cancelled`` status ledgered in the same transaction;
* **converging** — every exit path, exception included, reaches a terminal
  status. A run left in ``running`` is what the reaper exists to clean up, and
  needing the reaper is a bug rather than a design.

A handler therefore contains only the work. It receives a ``BackgroundTask`` and
must do exactly two things with it: ``await task.cancelled()`` before each unit,
and ``async with task.step(...)`` around it. Both orderings matter — checking
after opening the step emits a ``phase_started`` for work that will not happen,
which is indistinguishable in the timeline from work that silently failed.

**The supervisor re-reads the cancel flag after the handler returns.** A handler
that ignores ``cancelled()`` entirely still converges to ``cancelled`` rather
than reporting success for work somebody stopped. That belt-and-braces does not
make the cooperative check optional: a handler that ignores it runs to
completion first, which is the difference between stopping a sweep and letting
it finish and then relabelling it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from aleph_db.repos.agent_events import phase
from aleph_db.repos.agent_runs import SYSTEM_ACTOR
from aleph_db.repos.background_tasks import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    checkpoint,
    claim_ticket,
    finish_ticket,
)
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import start_span
from aleph_security.agent_token import mint_agent_token, verify_agent_token
from aleph_security.principal import Principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_log = structlog.get_logger(__name__)


class BackgroundTask:
    """What a handler is given: its scope, and the two verbs it must use.

    Deliberately thin. Everything that decides the run's fate lives in the
    supervisor below, so a handler cannot accidentally own status reporting —
    the pattern that produced a per-job copy of the same three-line finaliser in
    five worker modules, one of which was missing its failure branch.
    """

    def __init__(
        self,
        *,
        ctx: dict[str, Any],
        run_id: UUID,
        project_id: UUID,
        principal: Principal,
        params: dict[str, Any],
    ) -> None:
        self.ctx = ctx
        self.run_id = run_id
        self.project_id = project_id
        self.principal = principal
        self.params = params
        self.maker = ctx["session_maker"]
        #: Set the first time a checkpoint sees a cancellation. Read by the
        #: supervisor so a handler that returns early still lands on
        #: ``cancelled`` rather than ``succeeded``.
        self.cancel_seen = False

    async def cancelled(self) -> bool:
        """ "May I keep going?" — and a heartbeat on the same round trip.

        Call this *before* each unit of work. It reads a fresh session, so a
        cancellation the API committed one millisecond ago is visible even if
        the handler is holding a long transaction of its own.
        """
        result = await checkpoint(self.maker, run_id=self.run_id)
        if result.missing:
            # The row was deleted underneath us. Nothing to converge and nothing
            # to report to; stopping is the only honest option.
            self.cancel_seen = True
            return True
        if result.cancel_requested:
            self.cancel_seen = True
        return result.cancel_requested

    @asynccontextmanager
    async def step(self, name: str, **payload: Any) -> AsyncIterator[dict[str, Any]]:
        """One unit of work, bracketed by ``phase_started`` / ``phase_completed``.

        These are the rows ``GET /background-tasks/{id}`` reports as progress and
        the Inspector renders. Progress reported anywhere else would be a second
        account of the same work, free to disagree with this one.
        """
        async with phase(
            self.maker,
            agent_run_id=self.run_id,
            phase_name=name,
            started_payload=payload or None,
        ) as completed:
            yield completed

    def agent_token(self, *, ttl_seconds: int = 3600) -> str:
        """A token for a job this task fans out to, bound to **this** run.

        Bound to the ticket rather than to a fresh id so the money the fan-out
        spends attributes to the ticket that caused it. ``model_calls`` with a
        null ``agent_run_id`` is one of the eight numbers that define done, and a
        sweep that adds to it while repairing something else is not a repair.
        """
        return mint_agent_token(
            secret=self.ctx["agent_token_secret"],
            user_id=self.principal.user_id,
            project_id=self.project_id,
            agent_run_id=self.run_id,
            actor_kind="aleph_agent",
            correlation_id=f"bg-{self.run_id.hex}",
            ttl_seconds=ttl_seconds,
        )

    async def enqueue(self, function: str, *args: Any) -> None:
        await self.ctx["redis_pool"].enqueue_job(function, *args)


async def _converge(
    maker: Any,
    *,
    run_id: UUID,
    status: str,
    actor_id: UUID,
    actor_kind: str,
    result: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    """Terminal status + its ledger row, in one transaction, in its own session.

    Its own session because the failure path runs after an exception, and
    reusing a session whose transaction may already be poisoned is how a job
    fails to record that it failed.
    """
    async with maker() as session:
        await finish_ticket(
            session,
            LedgerWriter(session),
            run_id=run_id,
            status=status,
            actor_id=actor_id,
            actor_kind=actor_kind,
            result=result,
            error_text=error_text,
        )
        await session.commit()


async def background_task_job(
    ctx: dict[str, Any], agent_run_id_str: str, agent_token: str
) -> dict[str, Any]:
    from aleph_workers.jobs.background_kinds import BACKGROUND_TASK_HANDLERS

    secret: str = ctx["agent_token_secret"]
    run_id = UUID(agent_run_id_str)
    maker = ctx["session_maker"]
    try:
        claims = verify_agent_token(agent_token, secret=secret)
    except Exception as exc:
        # `mint_agent_token` refuses a TTL over one hour, so a ticket that waits
        # in the queue longer than that arrives with a token this worker cannot
        # verify. Raising here — before the run is claimed — would leave it at
        # `pending` forever while arq retried against the same dead token, and
        # the only evidence would be a stack trace in a worker log. Converging
        # it to a stated failure is the difference between a ticket that ends
        # and a ticket that disappears.
        _log.warning(
            "background_task.token_rejected",
            agent_run_id=agent_run_id_str,
            error=f"{type(exc).__name__}: {exc}",
        )
        await _converge(
            maker,
            run_id=run_id,
            status=STATUS_FAILED,
            actor_id=SYSTEM_ACTOR,
            actor_kind="system",
            error_text=(
                "dispatch token could not be verified (it expires one hour after "
                f"the ticket was created): {type(exc).__name__}: {exc}"
            ),
        )
        return {"ok": False, "error": "dispatch token rejected"}
    principal = Principal(
        user_id=claims.user_id,
        subject="agent",
        email="",
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )

    with start_span("worker.background_task", **{"aleph.agent_run_id": agent_run_id_str}):
        async with maker() as session:
            run, refusal = await claim_ticket(session, run_id=run_id)
            if run is None:
                # No row to converge and no project to ledger against. Raising
                # would have arq retry forever against a row that will never
                # appear.
                _log.warning("background_task.missing_run", agent_run_id=agent_run_id_str)
                return {"ok": False, "error": refusal}
            kind = run.agent_kind
            project_id = run.project_id
            params_raw = (run.input_payload or {}).get("params")
            params: dict[str, Any] = dict(params_raw) if isinstance(params_raw, dict) else {}
            if refusal is not None:
                interrupted = run.status not in ("succeeded", "failed", "cancelled")
                if interrupted:
                    # A prior attempt was killed mid-flight and arq re-enqueued.
                    # Re-running would duplicate the fan-out, so converge to a
                    # stated failure rather than leaving it running.
                    run.status = STATUS_FAILED
                    run.error_text = refusal
                await session.commit()
                _log.info(
                    "background_task.refused",
                    agent_run_id=agent_run_id_str,
                    reason=refusal,
                )
                return {"ok": False, "error": refusal}
            await session.commit()

        handler = BACKGROUND_TASK_HANDLERS.get(kind)
        if handler is None:
            # An accepted kind with no handler. The route validates against the
            # same vocabulary, so reaching here means the two have drifted —
            # which `test_every_kind_has_a_handler` exists to prevent, and which
            # must still fail loudly rather than hang as `running`.
            await _converge(
                maker,
                run_id=run_id,
                status=STATUS_FAILED,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                error_text=f"no handler registered for background task kind {kind!r}",
            )
            return {"ok": False, "error": f"no handler for kind {kind!r}"}

        task = BackgroundTask(
            ctx=ctx,
            run_id=run_id,
            project_id=project_id,
            principal=principal,
            params=params,
        )
        try:
            result = await handler(task)
        except Exception as exc:
            await _converge(
                maker,
                run_id=run_id,
                status=STATUS_FAILED,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                error_text=f"{type(exc).__name__}: {exc}",
            )
            raise

        # Re-read rather than trusting `cancel_seen`: a cancellation that landed
        # after the handler's last checkpoint is still a cancellation, and
        # reporting success for work somebody stopped is the failure this whole
        # workstream is about.
        final = await checkpoint(maker, run_id=run_id, heartbeat=False)
        cancelled = task.cancel_seen or final.cancel_requested
        status = STATUS_CANCELLED if cancelled else STATUS_SUCCEEDED
        await _converge(
            maker,
            run_id=run_id,
            status=status,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            result=result,
            error_text="cancelled on request" if cancelled else None,
        )

    _log.info(
        "worker.background_task.done",
        agent_run_id=agent_run_id_str,
        kind=kind,
        status=status,
    )
    return {"ok": not cancelled, "status": status, **result}
