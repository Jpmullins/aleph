"""Start / check / cancel work that takes longer than a conversation can wait.

The measured problem (`scripts/_acceptance/agent_turn_probe.py`): a tool-using
chat turn shows the analyst nothing for 16 to 20 seconds, because the assistant
calls a tool and blocks on it. The fix is not to make the slow work fast — a
corpus reindex is slow because it is large — it is to stop waiting on it. These
three routes are the general form of what ``start_research`` already does for
exactly one kind of work: dispatch, hand back a ticket, answer questions about
it later.

**Everything here is a tested HTTP route on purpose.** The assistant's tools
reach state only by self-calling routes with a minted agent token
(``copilot_agent._self_headers``), never by touching the database. A background
primitive built as a direct DB call would be the first agent capability outside
that rule, and the rule is what makes the agent's reach reviewable.

**The ticket is an ``AgentRun``.** Not a new table: the Inspector pane already
reads ``agent_runs`` + ``agent_events``, and a ticket the assistant hands back
has to appear in the same timeline as the turn that created it. The dispatch
also writes one ``AgentEvent`` against the parent chat run, so the conversation
records the hand-off rather than showing a silent gap.

**Ordering matters in two places and both are load-bearing.**

1. ``/kinds`` is declared before ``/{run_id}``. FastAPI matches routes in
   declaration order, so the reverse order sends "kinds" to the UUID parser and
   answers 422 for a route that exists.
2. The run row is committed *before* the job is enqueued. A worker that picks up
   a job whose row has not committed raises not-found, arq retries it, and —
   with the row still absent — nothing can ever mark it failed, so the ticket
   strands as ``pending`` forever. ``aleph_research.dispatch`` carries the same
   comment for the same reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Body, Path, Request, status
from pydantic import BaseModel, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound, ValidationFailed
from aleph_db.repos.background_tasks import (
    BACKGROUND_TASK_KINDS,
    STATUS_FAILED,
    Ticket,
    TicketView,
    create_ticket,
    finish_ticket,
    read_ticket,
    request_cancel,
)
from aleph_security.agent_token import mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

if TYPE_CHECKING:
    from datetime import datetime

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["background-tasks"])

#: The arq function every ticket is dispatched to. One supervisor for all kinds,
#: so cancellation, heartbeating and terminal-status reporting are written once
#: rather than once per job — the per-job copies are how `chunk_embed` ended up
#: with 45 runs stuck in `running`.
BACKGROUND_JOB = "background_task_job"

#: The longest `mint_agent_token` will issue (`agent_token._DEFAULT_TTL_SECONDS`
#: is a hard cap of one hour, not a default it will exceed on request).
#:
#: This is a real bound on the design and it is stated rather than worked
#: around: a ticket that sits in the queue for over an hour has a token the
#: worker cannot verify. `background_task_job` therefore treats an unverifiable
#: token as a terminal failure with a named reason, instead of raising before it
#: has claimed the run — which would leave the ticket at `pending` forever with
#: an arq retry loop as the only evidence. The token covers *claiming* the work,
#: not doing it; a long sweep mints its own fresh tokens per fan-out unit.
_TOKEN_TTL_SECONDS = 3600


class StartIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    #: The chat turn asking for the work. Optional because a human can start a
    #: task from the UI with no conversation attached, but the assistant always
    #: supplies it — that link is what lets the Inspector show one chain.
    parent_agent_run_id: UUID | None = None


class TicketOut(BaseModel):
    agent_run_id: str
    kind: str
    status: str
    correlation_id: str
    dispatched: bool


class TicketStatusOut(BaseModel):
    agent_run_id: str
    kind: str
    status: str
    terminal: bool
    cancel_requested: bool
    phase: str | None
    phase_event_kind: str | None
    phase_at: str | None
    started_at: str | None
    completed_at: str | None
    parent_agent_run_id: str | None
    result: dict[str, Any] | None
    error: str | None


class CancelOut(BaseModel):
    agent_run_id: str
    status: str
    outcome: str


class KindsOut(BaseModel):
    kinds: list[str]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _view_out(view: TicketView) -> TicketStatusOut:
    return TicketStatusOut(
        agent_run_id=str(view.run_id),
        kind=view.kind,
        status=view.status,
        terminal=view.terminal,
        cancel_requested=view.cancel_requested,
        phase=view.phase,
        phase_event_kind=view.phase_event_kind,
        phase_at=_iso(view.phase_at),
        started_at=_iso(view.started_at),
        completed_at=_iso(view.completed_at),
        parent_agent_run_id=(
            str(view.parent_agent_run_id) if view.parent_agent_run_id is not None else None
        ),
        result=view.result,
        error=view.error_text,
    )


async def _enqueue(request: Request, ticket: Ticket, *, token: str) -> None:
    """Put the job on the bus, reusing an injected pool when there is one.

    The API process does not mount the arq pool as a capability, so this opens
    one per dispatch exactly as ``/synthesize`` does. ``app.state.arq_pool`` is
    honoured when present so a test — and, later, a lifespan that does mount it
    — can supply one; the seam costs a getattr and removes the only reason this
    route would otherwise need a live Redis to be exercised at all.
    """
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is not None:
        await pool.enqueue_job(BACKGROUND_JOB, str(ticket.run_id), token)
        return
    settings = request.app.state.settings
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job(BACKGROUND_JOB, str(ticket.run_id), token)
    finally:
        await pool.aclose()


@router.get("/{project_id}/background-tasks/kinds", response_model=KindsOut)
async def list_kinds(project_id: ProjectScopeDep) -> KindsOut:
    """The kinds of work a ticket may name.

    Served rather than hardcoded in the assistant's prompt: a tool that offers a
    kind no worker can run produces a ticket that exists only to fail, and a
    prompt is the one place in this system nothing checks.
    """
    _ = project_id
    return KindsOut(kinds=list(BACKGROUND_TASK_KINDS))


@router.post(
    "/{project_id}/background-tasks",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketOut,
)
async def start_background_task(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[StartIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> TicketOut:
    """Create the ticket, commit it, enqueue the work. Returns immediately.

    202, not 200: nothing has been done yet, and a route that answered 200 for
    work that has not started is the beginning of an assistant that reports
    completion it cannot observe.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    if body.kind not in BACKGROUND_TASK_KINDS:
        msg = f"unknown background task kind {body.kind!r}; known kinds: {
            ', '.join(BACKGROUND_TASK_KINDS)
        }"
        raise ValidationFailed(msg)

    ticket = await create_ticket(
        session,
        ledger,
        project_id=project_id,
        kind=body.kind,
        params=body.params,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        parent_agent_run_id=body.parent_agent_run_id,
    )
    # Commit BEFORE enqueue — see the module docstring. `session_dep` would
    # commit after this handler returns, which is after the worker may already
    # have looked for the row.
    await session.commit()

    settings = request.app.state.settings
    token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=ticket.run_id,
        actor_kind="aleph_agent",
        correlation_id=ticket.correlation_id,
        ttl_seconds=_TOKEN_TTL_SECONDS,
    )
    try:
        await _enqueue(request, ticket, token=token)
    except Exception as exc:
        # The row is committed and no job will ever converge it. Fail it here so
        # it does not sit at `pending` forever looking like queued work — the
        # same recovery `dispatch_research` performs, and the reason a ticket
        # can be trusted to reach a terminal status.
        _log.exception("background_task.enqueue_failed", agent_run_id=str(ticket.run_id))
        await finish_ticket(
            session,
            ledger,
            run_id=ticket.run_id,
            status=STATUS_FAILED,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            error_text=f"enqueue failed: {type(exc).__name__}: {exc}",
        )
        await session.commit()
        raise

    return TicketOut(
        agent_run_id=str(ticket.run_id),
        kind=ticket.kind,
        status=ticket.status,
        correlation_id=ticket.correlation_id,
        dispatched=True,
    )


@router.get("/{project_id}/background-tasks/{run_id}", response_model=TicketStatusOut)
async def check_background_task(
    project_id: ProjectScopeDep,
    run_id: Annotated[UUID, Path(...)],
    session: SessionDep,
) -> TicketStatusOut:
    """Status plus the most recent phase this ticket recorded.

    The phase comes from ``agent_events``, the same rows the Inspector renders
    and the ``/agent-events`` SSE stream pushes. A progress string invented here
    would be a second account of the same work, free to disagree with the first.
    """
    view = await read_ticket(session, project_id=project_id, run_id=run_id)
    if view is None:
        msg = f"background task {run_id} not found"
        raise NotFound(msg)
    return _view_out(view)


@router.post("/{project_id}/background-tasks/{run_id}/cancel", response_model=CancelOut)
async def cancel_background_task(
    project_id: ProjectScopeDep,
    run_id: Annotated[UUID, Path(...)],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> CancelOut:
    """Ask the work to stop, and say honestly which of those two things happened.

    A queued ticket is cancelled here and now. A running one is *asked* to stop
    and converges at its next checkpoint. Reporting the second as the first is
    how a cancel button becomes a lie, so ``outcome`` distinguishes them and the
    assistant is expected to repeat the distinction.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    outcome = await request_cancel(
        session,
        ledger,
        project_id=project_id,
        run_id=run_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
    )
    if outcome is None:
        msg = f"background task {run_id} not found"
        raise NotFound(msg)
    return CancelOut(
        agent_run_id=str(outcome.run_id), status=outcome.status, outcome=outcome.outcome
    )
