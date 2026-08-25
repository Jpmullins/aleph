"""Aleph hosts the Agent Protocol, so deepagents drives Aleph's own queue.

`docs/decisions.md` D17. `deepagents`' `AsyncSubAgentMiddleware` is documented to
talk to "any server that implements the Agent Protocol… or self-host any Agent
Protocol-compatible server", and the surface it actually uses is **five routes**
— `threads.create`, `threads.get`, `runs.create`, `runs.get`, `runs.cancel`.

Implementing those five against `agent_threads` + `agent_runs` means Aleph gets,
without writing any of it: the five supervisor tools (`start_async_task`,
`check_async_task`, `update_async_task`, `cancel_async_task`,
`list_async_tasks`); the dedicated `async_tasks` state channel, which exists so
task ids **survive context compaction** when they would otherwise be lost with
the tool messages that carried them; the system-prompt rules that stop a
supervisor polling immediately after launch and turning async back into
blocking; and `update`'s interrupt-multitask semantics for steering a task
mid-flight.

**Scope comes from the credential, not the path.** Every other Aleph route is
`/v1/projects/{project_id}/…`, but the SDK builds its own URLs — it posts to
`{base_url}/threads` and nothing else — so there is no path segment to put a
project in. The project is therefore read from the agent token's signed
`project_id` claim (`Principal.project_id`), which is exactly what that claim is
for, and a principal without one is refused. That is stricter than the usual
route, not looser: a human session token has `project_id = None` and cannot
reach these routes at all.

**Threads are not chat threads.** `assistant_threads.session_id` is NOT NULL and
a delegated subagent run is not a user chat session; reusing it would mean
minting a fake session per delegation to satisfy a foreign concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_core.errors import NotFound, PermissionDenied, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.agent_protocol import ProtocolStatus, to_protocol_status
from aleph_db.models.agent import AgentRun, AgentThread
from aleph_observability.tracing import current_trace_id
from aleph_security.agent_token import mint_agent_token

if TYPE_CHECKING:
    from aleph_security.principal import Principal

router = APIRouter(prefix="/v1/agent-protocol", tags=["agent-protocol"])

#: The arq job that runs a delegated subagent.
DELEGATION_JOB = "delegated_subagent_job"

#: Ceiling on a delegated run's token. Long enough for real work, short enough
#: that a leaked token is not an indefinite credential.
_TOKEN_TTL_SECONDS = 3600


class ThreadOut(BaseModel):
    """`threads.create` / `threads.get`.

    `values` is the contract, not an implementation detail:
    `_build_check_result` reads a finished task's output from
    `thread["values"]["messages"]` and reports nothing if it is absent.
    """

    model_config = ConfigDict(populate_by_name=True)

    thread_id: str
    values: dict[str, Any] = Field(default_factory=dict)


class RunIn(BaseModel):
    """`runs.create`. `assistant_id` is the `graph_id` from the AsyncSubAgent spec."""

    assistant_id: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    #: `update_async_task` sends `"interrupt"`. Accepted and recorded; Aleph
    #: cancels the in-flight run rather than queueing behind it, which is what
    #: "interrupt" means and what the supervisor is told happened.
    multitask_strategy: str | None = None


class RunOut(BaseModel):
    run_id: str
    thread_id: str
    status: ProtocolStatus


def _project_of(principal: Principal) -> UUID:
    """The project this credential is bound to, or a refusal.

    Deliberately not a membership lookup: these routes are reached only by a
    minted agent token, whose `project_id` claim IS the scope. A principal with
    no bound project has arrived by some other door and is refused before any
    row is read.
    """
    if principal.project_id is None:
        msg = (
            "the agent protocol is reachable only with a project-scoped agent "
            "token; this credential is bound to no project"
        )
        raise PermissionDenied(msg)
    return principal.project_id


async def _thread_or_404(session: SessionDep, thread_id: UUID, project_id: UUID) -> AgentThread:
    row = (
        await session.execute(
            select(AgentThread).where(
                AgentThread.id == thread_id, AgentThread.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # Scoped by project in the same query, so a thread in another project is
        # indistinguishable from one that does not exist. That is the intent.
        msg = f"no agent thread {thread_id}"
        raise NotFound(msg)
    return row


@router.post("/threads", response_model=ThreadOut)
async def create_thread(
    session: SessionDep,
    principal: PrincipalDep,
    # The SDK posts `{}` or a metadata object; nothing in it is load-bearing.
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> ThreadOut:
    """`threads.create()`. Takes no arguments that matter, and returns an id.

    `graph_id` is left NULL here on purpose: the SDK creates the thread first
    and only names an `assistant_id` on the first `runs.create`. Filling it now
    would mean inventing a value.
    """
    project_id = _project_of(principal)
    thread = AgentThread(
        id=uuid7(),
        project_id=project_id,
        graph_id=None,
        parent_agent_run_id=principal.agent_run_id,
        values_jsonb={},
        created_by=principal.user_id,
        trace_id=current_trace_id(),
    )
    session.add(thread)
    await session.commit()
    return ThreadOut(thread_id=str(thread.id), values={})


@router.get("/threads/{thread_id}", response_model=ThreadOut)
async def get_thread(thread_id: UUID, session: SessionDep, principal: PrincipalDep) -> ThreadOut:
    """`threads.get()`. The supervisor reads a finished task's output from here."""
    thread = await _thread_or_404(session, thread_id, _project_of(principal))
    return ThreadOut(thread_id=str(thread.id), values=thread.values_jsonb or {})


@router.post("/threads/{thread_id}/runs", response_model=RunOut)
async def create_run(
    thread_id: UUID,
    body: RunIn,
    request: Request,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> RunOut:
    """`runs.create()`. Starts a delegated subagent and returns immediately.

    Returning before the work is done is the whole feature: the supervisor gets
    a task id and keeps talking to the user.
    """
    from aleph_api.subagents import DELEGATABLE_SUBAGENTS

    project_id = _project_of(principal)
    thread = await _thread_or_404(session, thread_id, project_id)

    if body.assistant_id not in DELEGATABLE_SUBAGENTS:
        # An allowlist, not a lookup. A supervisor naming an arbitrary graph is
        # the same shape of hazard as a client naming an arbitrary project, and
        # the refusal names what IS available so the model can correct itself.
        msg = (
            f"unknown assistant_id {body.assistant_id!r}; "
            f"delegatable subagents are: {', '.join(sorted(DELEGATABLE_SUBAGENTS))}"
        )
        raise ValidationFailed(msg)

    # `update` interrupts rather than queues. The supervisor is told the task
    # restarted with its new instructions, so leaving the old run live would
    # spend a second model loop nobody is reading.
    if body.multitask_strategy == "interrupt":
        for live in (
            await session.execute(
                select(AgentRun).where(
                    AgentRun.agent_thread_id == thread.id,
                    AgentRun.status.in_(("pending", "running")),
                )
            )
        ).scalars():
            live.status = "cancelled"
            live.completed_at = utcnow()

    thread.graph_id = body.assistant_id
    run = AgentRun(
        id=uuid7(),
        project_id=project_id,
        agent_kind=f"delegation:{body.assistant_id}",
        correlation_id=str(uuid7()),
        status="pending",
        input_payload=body.input or {},
        agent_thread_id=thread.id,
        created_by=principal.user_id,
        trace_id=current_trace_id(),
    )
    session.add(run)
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="delegation.start",
        target_id=run.id,
        target_kind="agent_run",
        payload={"assistant_id": body.assistant_id, "thread_id": str(thread.id)},
        trace_id=current_trace_id(),
    )
    # Commit BEFORE enqueue: the worker looks the row up by id, and arq can hand
    # the job to a worker before this handler's own transaction would close.
    await session.commit()

    token = mint_agent_token(
        secret=request.app.state.settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=run.id,
        actor_kind="aleph_agent",
        correlation_id=run.correlation_id,
        ttl_seconds=_TOKEN_TTL_SECONDS,
    )
    await _enqueue(request, run_id=run.id, token=token)
    return RunOut(run_id=str(run.id), thread_id=str(thread.id), status="pending")


@router.get("/threads/{thread_id}/runs/{run_id}", response_model=RunOut)
async def get_run(
    thread_id: UUID, run_id: UUID, session: SessionDep, principal: PrincipalDep
) -> RunOut:
    """`runs.get()`. The status word here is the whole contract — see D17."""
    project_id = _project_of(principal)
    run = (
        await session.execute(
            select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.agent_thread_id == thread_id,
                AgentRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        msg = f"no run {run_id} on thread {thread_id}"
        raise NotFound(msg)
    return RunOut(
        run_id=str(run.id), thread_id=str(thread_id), status=to_protocol_status(run.status)
    )


@router.post("/threads/{thread_id}/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    thread_id: UUID,
    run_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> RunOut:
    """`runs.cancel()`. Idempotent, and it does not resurrect a finished run.

    Cancelling something that already succeeded reports its real status rather
    than overwriting it — the supervisor asked to stop work that is no longer
    running, and telling it `cancelled` would misreport a result that exists.
    """
    project_id = _project_of(principal)
    run = (
        await session.execute(
            select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.agent_thread_id == thread_id,
                AgentRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        msg = f"no run {run_id} on thread {thread_id}"
        raise NotFound(msg)

    if run.status in ("pending", "running"):
        run.status = "cancelled"
        run.completed_at = utcnow()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="delegation.cancel",
            target_id=run.id,
            target_kind="agent_run",
            payload={"thread_id": str(thread_id)},
            trace_id=current_trace_id(),
        )
        await session.commit()
    return RunOut(
        run_id=str(run.id), thread_id=str(thread_id), status=to_protocol_status(run.status)
    )


async def _enqueue(request: Request, *, run_id: UUID, token: str) -> None:
    """Put the delegation on the bus, reusing an injected pool when there is one.

    Same seam as `background_tasks._enqueue`: the API does not mount the arq
    pool as a capability, and honouring `app.state.arq_pool` is what lets a test
    exercise this route without a live Redis.
    """
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is not None:
        await pool.enqueue_job(DELEGATION_JOB, str(run_id), token)
        return
    settings = request.app.state.settings
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job(DELEGATION_JOB, str(run_id), token)
    finally:
        await pool.aclose()
