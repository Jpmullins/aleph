"""Long jobs return a ticket, and the ticket tells the truth (WS-H6).

The measured problem: a tool-using chat turn shows the analyst nothing for 16 to
20 seconds, because the assistant calls a tool and waits on it inline. The only
existing escape hatch is ``start_research``, hardcoded for one kind of work.
These tests cover the general form — start, check, cancel — and the four things
that are easy to fake about it:

* **start returns before the work does.** Trivially true of any route that
  forgets to enqueue anything, so the ticket is also asserted to be a real
  ``agent_runs.id`` that a worker can still act on.
* **check reports the phase the work actually recorded**, read out of
  ``agent_events`` — the same rows the Inspector renders. A progress string
  invented by the check route would be a second account of the same work, free
  to disagree with the first.
* **cancel stops the work.** A flag nobody reads looks exactly like a flag that
  works until it is tested against a job that is genuinely mid-flight, which is
  why the cancel test drives the real supervisor with the handler paused at a
  known point rather than racing a sleep.
* **the ticket is linked to the conversation that asked for it**, in both
  directions, so the Inspector can show one chain instead of two islands.

Plus the interaction WS-H6 was told to check and nobody had: the stale-run
reaper fails anything sitting in ``running`` for an hour, which is exactly what
a legitimate two-hour sweep looks like from the outside.

**What is a fixture here and what is production.** The arq pool is faked (it is
an external message bus, and no worker runs in the test process) and one test
registers a handler of its own so the loop can be paused at a known point.
Everything being asserted — claiming, heartbeating, the cancellation check, the
terminal status, the ledger row, the phase events, the routes — is production
code from ``aleph_db.repos.background_tasks``,
``aleph_workers.jobs.background`` and ``aleph_api.routes.background_tasks``.
A handler supplies the *work*; it supplies none of the mechanism under test.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Path
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aleph_api.deps import principal_dep
from aleph_api.middleware.project_scope import project_scope_dep
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentEvent, AgentRun
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.repos.agent_runs import stale_running_runs
from aleph_db.repos.background_tasks import (
    ACTION_CANCEL_REQUESTED,
    ACTION_CANCELLED,
    ACTION_DISPATCH,
    BACKGROUND_TASK_KINDS,
    DISPATCH_EVENT_KIND,
    HEARTBEAT_KEY,
    PARENT_RUN_KEY,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    create_ticket,
    finish_ticket,
    request_cancel,
)
from aleph_db.repos.ledger import LedgerWriter
from aleph_rks.models import NormalizedDocument
from aleph_security.agent_token import mint_agent_token, verify_agent_token
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole
from aleph_workers.jobs.background import background_task_job
from aleph_workers.jobs.background_kinds import BACKGROUND_TASK_HANDLERS

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000c6")

#: Settings refuse short or placeholder secrets at boot; this is a real 48-char
#: value so the app the tests build is shaped like the one that ships.
SECRET = "ws-h6-integration-secret-0123456789abcdef01234567"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _RecordingPool:
    """Stands in for the arq Redis pool.

    A message bus, not the thing under test: no worker runs in this process, so
    a real enqueue would put a job on a queue nothing drains. What matters to
    every assertion below is that the route enqueued *something* and returned
    without waiting for it, and that is exactly what this records.
    """

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any) -> None:
        self.jobs.append((function, args))


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    principal: Principal,
    pool: _RecordingPool,
) -> FastAPI:
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    app.state.settings = SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret=SECRET,
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    )
    app.state.session_maker = maker
    app.state.arq_pool = pool

    async def _fake_local_dev(_request: Any) -> Principal:
        return principal

    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)

    async def _scope(
        project_id: Annotated[uuid.UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> uuid.UUID:
        p.cache_role(project_id, ProjectRole.OWNER.value)
        return project_id

    app.dependency_overrides[project_scope_dep] = _scope
    return app


def _build_app_as(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    principal: Principal,
    pool: _RecordingPool,
    role: ProjectRole,
) -> FastAPI:
    """`_build_app`, but the caller holds `role` rather than OWNER.

    Every other test in this file caches OWNER, so `require_at_least(...,
    EDITOR)` on the two mutating routes was never executed: deleting both calls
    outright left 161 integration tests green. A role gate no test can fail is
    an assumption, not a gate.
    """
    app = _build_app(monkeypatch, maker, principal, pool)

    async def _scope(
        project_id: Annotated[uuid.UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> uuid.UUID:
        p.cache_role(project_id, role.value)
        return project_id

    app.dependency_overrides[project_scope_dep] = _scope
    return app


def _principal() -> Principal:
    return Principal(
        user_id=ACTOR,
        subject="local-dev",
        email="dev@example.com",
        actor_kind="user",
    )


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _worker_ctx(maker: Callable[[], AsyncSession], pool: _RecordingPool) -> dict[str, Any]:
    """The arq context, with exactly the keys `background_task_job` reads."""
    return {"session_maker": maker, "agent_token_secret": SECRET, "redis_pool": pool}


def _token(project_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return mint_agent_token(
        secret=SECRET,
        user_id=ACTOR,
        project_id=project_id,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id=f"bg-{run_id.hex}",
        ttl_seconds=3600,
    )


async def _make_ticket(
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    kind: str,
    *,
    params: dict[str, Any] | None = None,
    parent: uuid.UUID | None = None,
) -> uuid.UUID:
    async with maker() as session:
        ticket = await create_ticket(
            session,
            LedgerWriter(session),
            project_id=project_id,
            kind=kind,
            params=params or {},
            actor_id=ACTOR,
            actor_kind="user",
            parent_agent_run_id=parent,
        )
        await session.commit()
    return ticket.run_id


async def _run(maker: Callable[[], AsyncSession], run_id: uuid.UUID) -> AgentRun | None:
    async with maker() as session:
        return (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()


async def _events(maker: Callable[[], AsyncSession], run_id: uuid.UUID) -> list[AgentEvent]:
    async with maker() as session:
        rows = await session.execute(
            select(AgentEvent)
            .where(AgentEvent.agent_run_id == run_id)
            .order_by(AgentEvent.timestamp, AgentEvent.id)
        )
        return list(rows.scalars().all())


async def _ledger_kinds(maker: Callable[[], AsyncSession], target_id: uuid.UUID) -> list[str]:
    async with maker() as session:
        rows = await session.execute(
            select(ActionLedgerEvent.action_kind)
            .where(ActionLedgerEvent.target_id == target_id)
            .order_by(ActionLedgerEvent.timestamp)
        )
        return [r[0] for r in rows.all()]


async def _seed_unindexed_documents(
    maker: Callable[[], AsyncSession], project_id: uuid.UUID, count: int
) -> None:
    """Normalized documents with no chunks — what `reindex_corpus` selects.

    No source rows: `normalized_documents` declares no foreign keys (verified in
    the inc1 migration), and the handler joins on the absence of chunks only.
    """
    async with maker() as session:
        for i in range(count):
            session.add(
                NormalizedDocument(
                    id=uuid7(),
                    project_id=project_id,
                    source_id=uuid7(),
                    source_version_id=uuid7(),
                    markdown_uri=f"mem://ws-h6/{i}.md",
                    parser="test",
                    parser_version="1",
                    char_count=10,
                    token_count=3,
                    created_by=ACTOR,
                )
            )
        await session.commit()


@pytest.fixture
def slow_kind() -> AsyncIterator[dict[str, Any]]:
    """A handler that pauses mid-sweep so a cancel can land at a known point.

    Registered into the real registry and removed afterwards. It supplies the
    unit of work and nothing else: the checkpoint it calls, the phase events it
    emits and the status it ends on are all production code.
    """
    kind = "__ws_h6_slow__"
    control: dict[str, Any] = {
        "kind": kind,
        "reached_first_unit": asyncio.Event(),
        "release": asyncio.Event(),
        "units": 6,
        "completed": 0,
    }

    async def _slow(task: Any) -> dict[str, Any]:
        for i in range(control["units"]):
            # Before the step, never inside it — a phase_started for work that
            # will not happen is indistinguishable from work that failed
            # silently.
            if await task.cancelled():
                break
            async with task.step(f"unit_{i}"):
                control["completed"] += 1
            if i == 0:
                control["reached_first_unit"].set()
                await control["release"].wait()
        return {"units_completed": control["completed"]}

    BACKGROUND_TASK_HANDLERS[kind] = _slow
    try:
        yield control
    finally:
        BACKGROUND_TASK_HANDLERS.pop(kind, None)


# ---------------------------------------------------------------------------
# Criterion 1 — a ticket comes back before the work does
# ---------------------------------------------------------------------------


async def test_start_returns_immediately(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """POST returns a usable ticket in well under two seconds, work unstarted.

    Both halves matter. The wall clock alone would pass for a route that
    enqueues nothing at all, so the ticket is also resolved back to a real
    `agent_runs` row in a non-terminal status, and the job is asserted to be on
    the bus with the run id and a token bound to it.
    """
    pool = _RecordingPool()
    app = _build_app(monkeypatch, maker, _principal(), pool)
    async with await _client(app) as client:
        started = time.monotonic()
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={"kind": "reindex_corpus", "params": {}},
        )
        elapsed = time.monotonic() - started

    assert resp.status_code == 202, resp.text
    assert elapsed < 2.0, f"start took {elapsed:.2f}s"
    body = resp.json()
    run_id = uuid.UUID(body["agent_run_id"])

    run = await _run(maker, run_id)
    assert run is not None, "the ticket does not name a real agent_runs row"
    assert run.status in (STATUS_PENDING, STATUS_RUNNING)
    assert run.project_id == committed_project
    assert run.agent_kind == "reindex_corpus"

    assert len(pool.jobs) == 1, "the work was not put on the bus"
    function, args = pool.jobs[0]
    assert function == "background_task_job"
    assert args[0] == str(run_id)
    # The job carries a token bound to the ticket, so the worker can act as the
    # requester and the fan-out's spend attributes to this run.
    claims = verify_agent_token(args[1], secret=SECRET)
    assert claims.agent_run_id == run_id
    assert claims.project_id == committed_project

    assert ACTION_DISPATCH in await _ledger_kinds(maker, run_id)


async def test_start_refuses_a_kind_no_worker_can_run(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """An unknown kind is refused at the door, and nothing is enqueued.

    A ticket for a kind no handler is bound to can only ever fail, and it would
    fail minutes later in a worker log rather than in the assistant's hand.
    """
    pool = _RecordingPool()
    app = _build_app(monkeypatch, maker, _principal(), pool)
    async with await _client(app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={"kind": "make_me_a_sandwich", "params": {}},
        )
        kinds = await client.get(f"/v1/projects/{committed_project}/background-tasks/kinds")

    assert resp.status_code == 422, resp.text
    assert pool.jobs == []
    assert kinds.status_code == 200
    assert kinds.json()["kinds"] == list(BACKGROUND_TASK_KINDS)


# ---------------------------------------------------------------------------
# Criterion 2 — checking reports real progress
# ---------------------------------------------------------------------------


async def test_check_reports_phases(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """The check route names the latest `agent_events` phase, then goes terminal.

    Driven through the real `reindex_corpus` handler over two seeded documents,
    so the phases being reported are ones production work emitted rather than
    ones the test wrote.
    """
    await _seed_unindexed_documents(maker, committed_project, 2)
    pool = _RecordingPool()
    app = _build_app(monkeypatch, maker, _principal(), pool)

    async with await _client(app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={"kind": "reindex_corpus"},
        )
        run_id = uuid.UUID(resp.json()["agent_run_id"])

        before = await client.get(f"/v1/projects/{committed_project}/background-tasks/{run_id}")
        assert before.status_code == 200
        assert before.json()["status"] == STATUS_PENDING
        assert before.json()["terminal"] is False
        assert before.json()["phase"] is None, "a pending ticket has no progress to report"

        result = await background_task_job(
            _worker_ctx(maker, pool), str(run_id), _token(committed_project, run_id)
        )

        after = await client.get(f"/v1/projects/{committed_project}/background-tasks/{run_id}")

    assert result["ok"] is True
    body = after.json()
    assert body["status"] == STATUS_SUCCEEDED
    assert body["terminal"] is True
    assert body["result"]["documents_enqueued"] == 2

    events = await _events(maker, run_id)
    phases = [(e.event_kind, (e.payload_jsonb or {}).get("phase")) for e in events]
    assert ("phase_started", "plan") in phases
    assert phases.count(("phase_started", "index_document")) == 2

    # The reported phase is the most recent row, not a guess: it must be the
    # last event this run actually wrote.
    assert body["phase"] == (events[-1].payload_jsonb or {}).get("phase")
    assert body["phase_event_kind"] == events[-1].event_kind

    # And the work really was handed on, with a token bound to the ticket so the
    # fan-out's spend attributes to it.
    fanned = [j for j in pool.jobs if j[0] == "chunk_embed_job"]
    assert len(fanned) == 2


# ---------------------------------------------------------------------------
# Criterion 3 — cancelling actually stops the work, and is auditable
# ---------------------------------------------------------------------------


async def test_cancel_stops_the_job(
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    slow_kind: dict[str, Any],
) -> None:
    """A running ticket stops at its next checkpoint, and says so in the ledger.

    Deterministic rather than raced: the handler completes unit 0 and then waits
    on an event. The cancel is requested while it waits, so "the next checkpoint
    sees it" is a fact about the code rather than about scheduling luck.
    """
    pool = _RecordingPool()
    run_id = await _make_ticket(maker, committed_project, slow_kind["kind"])
    ctx = _worker_ctx(maker, pool)

    job = asyncio.create_task(
        background_task_job(ctx, str(run_id), _token(committed_project, run_id))
    )
    await asyncio.wait_for(slow_kind["reached_first_unit"].wait(), timeout=20)

    async with maker() as session:
        outcome = await request_cancel(
            session,
            LedgerWriter(session),
            project_id=committed_project,
            run_id=run_id,
            actor_id=ACTOR,
            actor_kind="user",
        )
        await session.commit()
        # Postgres' own clock, so the "nothing started after this" assertion
        # below cannot be broken by skew between this process and the database.
        cancel_ts = (await session.execute(select(func.now()))).scalar_one()

    assert outcome is not None
    assert outcome.outcome == "cancel_requested", (
        "a running job cannot be stopped by the API alone; saying otherwise "
        "reports work as finished while it is still running"
    )

    slow_kind["release"].set()
    result = await asyncio.wait_for(job, timeout=30)

    run = await _run(maker, run_id)
    assert run is not None
    assert run.status == STATUS_CANCELLED
    assert run.completed_at is not None
    assert result["ok"] is False

    events = await _events(maker, run_id)
    started_units = [
        (e.payload_jsonb or {}).get("phase") for e in events if e.event_kind == "phase_started"
    ]
    # The handler was willing to run six units. It ran exactly one, and nothing
    # was started after the cancel landed.
    assert started_units == ["unit_0"], started_units
    assert slow_kind["completed"] == 1
    late = [e for e in events if e.event_kind == "phase_started" and e.timestamp > cancel_ts]
    assert late == [], f"{len(late)} phases started after the cancellation"

    kinds = await _ledger_kinds(maker, run_id)
    assert ACTION_CANCEL_REQUESTED in kinds
    assert ACTION_CANCELLED in kinds


async def test_cancelling_a_queued_ticket_stops_it_before_it_starts(
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """A ticket cancelled while queued never runs, and the worker no-ops.

    The branch that is easy to leave out: nothing is executing, so nothing will
    ever notice a cooperative flag. If the API does not resolve it here the
    ticket sits at `pending` until a worker picks it up and does the work
    somebody asked it not to.
    """
    pool = _RecordingPool()
    await _seed_unindexed_documents(maker, committed_project, 2)
    run_id = await _make_ticket(maker, committed_project, "reindex_corpus")

    async with maker() as session:
        outcome = await request_cancel(
            session,
            LedgerWriter(session),
            project_id=committed_project,
            run_id=run_id,
            actor_id=ACTOR,
            actor_kind="user",
        )
        await session.commit()
    assert outcome is not None
    assert outcome.outcome == "cancelled"

    result = await background_task_job(
        _worker_ctx(maker, pool), str(run_id), _token(committed_project, run_id)
    )

    assert result["ok"] is False
    run = await _run(maker, run_id)
    assert run is not None
    assert run.status == STATUS_CANCELLED
    assert await _events(maker, run_id) == [], "cancelled work still emitted phases"
    assert pool.jobs == [], "cancelled work was still fanned out"
    assert ACTION_CANCELLED in await _ledger_kinds(maker, run_id)


async def test_cancellation_and_its_ledger_row_are_one_transaction(
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """Roll the transaction back and BOTH the status and the audit row vanish.

    The standing rule is "every state mutation writes an ActionLedgerEvent in
    the same transaction", and the usual test — assert both rows exist — passes
    just as happily for two separate commits, where a crash between them leaves
    a cancelled run nobody can account for. Rolling back is what distinguishes
    them.
    """
    run_id = await _make_ticket(maker, committed_project, "review_sweep")

    async with maker() as session:
        written = await finish_ticket(
            session,
            LedgerWriter(session),
            run_id=run_id,
            status=STATUS_CANCELLED,
            actor_id=ACTOR,
            actor_kind="user",
            error_text="cancelled on request",
        )
        assert written is True
        await session.flush()
        # Both are visible inside the transaction...
        assert (
            await session.execute(
                select(ActionLedgerEvent).where(
                    ActionLedgerEvent.target_id == run_id,
                    ActionLedgerEvent.action_kind == ACTION_CANCELLED,
                )
            )
        ).scalar_one_or_none() is not None
        await session.rollback()

    # ...and neither survives it.
    run = await _run(maker, run_id)
    assert run is not None
    assert run.status == STATUS_PENDING
    assert ACTION_CANCELLED not in await _ledger_kinds(maker, run_id)


# ---------------------------------------------------------------------------
# Criterion 4 — the ticket is linked to the conversation that started it
# ---------------------------------------------------------------------------


async def test_parent_link(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """Chat turn → ticket → phases, reconstructable from the two read paths.

    Forward: the background run records the chat turn's id in `input_payload`.
    Backward: the chat turn's own event stream carries the hand-off, so
    `GET /agent-events?agent_run_id=<chat run>` shows that a ticket was created
    and names it. Without the backward half the Inspector shows a conversation
    with a silent gap where the delegation was.
    """
    chat_run_id = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=chat_run_id,
                project_id=committed_project,
                agent_kind="assistant",
                correlation_id=f"chat-{chat_run_id.hex}",
                status=STATUS_RUNNING,
                started_at=utcnow(),
                input_payload={"thread_id": f"proj:{committed_project}:t1"},
                created_by=ACTOR,
            )
        )
        await session.commit()

    pool = _RecordingPool()
    app = _build_app(monkeypatch, maker, _principal(), pool)
    async with await _client(app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={"kind": "review_sweep", "parent_agent_run_id": str(chat_run_id)},
        )
        assert resp.status_code == 202, resp.text
        child_id = uuid.UUID(resp.json()["agent_run_id"])

        check = await client.get(f"/v1/projects/{committed_project}/background-tasks/{child_id}")
        parent_events = await client.get(
            f"/v1/projects/{committed_project}/agent-events",
            params={"agent_run_id": str(chat_run_id)},
        )

    child = await _run(maker, child_id)
    assert child is not None
    assert child.input_payload[PARENT_RUN_KEY] == str(chat_run_id)
    assert check.json()["parent_agent_run_id"] == str(chat_run_id)

    assert parent_events.status_code == 200, parent_events.text
    handoffs = [e for e in parent_events.json() if e["event_kind"] == DISPATCH_EVENT_KIND]
    assert len(handoffs) == 1, "the conversation's timeline does not record the hand-off"
    assert handoffs[0]["payload"]["child_agent_run_id"] == str(child_id)
    assert handoffs[0]["agent_kind"] == "assistant"


async def test_a_ticket_from_another_project_is_not_readable(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """A run id is a bare UUID anything could hand the agent.

    The check and cancel routes scope by project in the WHERE clause rather than
    resolving the run first, so a ticket id learned from an ingested page cannot
    be used to read or stop another project's work.
    """
    other_project = uuid.uuid4()
    foreign_run = await _make_ticket(maker, other_project, "review_sweep")
    pool = _RecordingPool()
    app = _build_app(monkeypatch, maker, _principal(), pool)
    try:
        async with await _client(app) as client:
            got = await client.get(
                f"/v1/projects/{committed_project}/background-tasks/{foreign_run}"
            )
            stopped = await client.post(
                f"/v1/projects/{committed_project}/background-tasks/{foreign_run}/cancel"
            )
        assert got.status_code == 404
        assert stopped.status_code == 404
        run = await _run(maker, foreign_run)
        assert run is not None
        assert run.status == STATUS_PENDING, "a cross-project cancel changed the row"
    finally:
        async with maker() as session:
            await session.execute(delete(AgentRun).where(AgentRun.project_id == other_project))
            await session.commit()


# ---------------------------------------------------------------------------
# The reaper interaction WS-H6 was told to check
# ---------------------------------------------------------------------------


async def test_a_heartbeating_task_is_not_reaped_as_stale(
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """A long task that is alive survives the reaper; one that died does not.

    `reap_stale_runs` fails anything in `running` whose `started_at` is over an
    hour old, on the assumption the owning process died. That assumption is
    false for exactly the work this workstream introduces: a corpus reindex or a
    review sweep can legitimately run for hours, and being reaped mid-flight
    would mark working work `failed`, tell the analyst it died, and leave the
    real process writing to a row that says it is over.

    Both directions are asserted, because a filter that exempts everything is
    the same defect wearing the other face.
    """
    long_ago = utcnow() - timedelta(hours=3)
    alive_id, dead_id = uuid7(), uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=alive_id,
                project_id=committed_project,
                agent_kind="reindex_corpus",
                correlation_id=f"bg-{alive_id.hex}",
                status=STATUS_RUNNING,
                started_at=long_ago,
                input_payload={"kind": "reindex_corpus"},
                result_payload={HEARTBEAT_KEY: utcnow().isoformat()},
                created_by=ACTOR,
            )
        )
        session.add(
            AgentRun(
                id=dead_id,
                project_id=committed_project,
                agent_kind="reindex_corpus",
                correlation_id=f"bg-{dead_id.hex}",
                status=STATUS_RUNNING,
                started_at=long_ago,
                input_payload={"kind": "reindex_corpus"},
                result_payload={HEARTBEAT_KEY: long_ago.isoformat()},
                created_by=ACTOR,
            )
        )
        await session.commit()

    async with maker() as session:
        stale_ids = {r.id for r in await stale_running_runs(session)}

    assert dead_id in stale_ids, "a task whose last heartbeat is 3h old is dead"
    assert alive_id not in stale_ids, (
        "a task that heartbeat seconds ago was about to be reaped as dead"
    )


async def test_the_worker_heartbeats_while_it_works(
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    slow_kind: dict[str, Any],
) -> None:
    """The heartbeat is written by the checkpoint the handler already calls.

    The exemption above is only worth having if something writes the beat. This
    pins that the supervisor does it as part of claiming the run and again at
    every cancellation check, rather than needing a handler to remember.
    """
    pool = _RecordingPool()
    run_id = await _make_ticket(maker, committed_project, slow_kind["kind"])
    job = asyncio.create_task(
        background_task_job(
            _worker_ctx(maker, pool), str(run_id), _token(committed_project, run_id)
        )
    )
    await asyncio.wait_for(slow_kind["reached_first_unit"].wait(), timeout=20)

    mid = await _run(maker, run_id)
    assert mid is not None
    assert mid.status == STATUS_RUNNING
    assert isinstance((mid.result_payload or {}).get(HEARTBEAT_KEY), str), (
        "a running task with no heartbeat is indistinguishable from a dead one"
    )

    slow_kind["release"].set()
    await asyncio.wait_for(job, timeout=30)
    done = await _run(maker, run_id)
    assert done is not None
    assert done.status == STATUS_SUCCEEDED
    assert done.result_payload is not None
    assert done.result_payload["units_completed"] == slow_kind["units"]


async def test_a_crash_mid_flight_does_not_strand_the_ticket(
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """The failure drill, in the two shapes a worker actually dies in.

    WS-H6's review step asks: kill the arq worker while a background run is in
    flight, and confirm the run does not sit at `running` forever. There are
    exactly two ways that plays out, and both are covered here rather than by
    stopping a shared container:

    (a) the work raises — the supervisor converges to `failed` with the error
        text, in one transaction with its ledger row, and re-raises so arq sees
        the failure;
    (b) the process is killed and arq re-delivers the job — the run is already
        `running`, so re-running it would duplicate the fan-out. The supervisor
        converges it to `failed` with a stated reason instead.

    The third shape — the process is killed and the job is never re-delivered —
    is the reaper's, and it is covered by
    `test_a_heartbeating_task_is_not_reaped_as_stale`, whose dead run is exactly
    a heartbeat that stopped.
    """
    pool = _RecordingPool()
    kind = "__ws_h6_explodes__"

    async def _explode(task: Any) -> dict[str, Any]:
        async with task.step("plan"):
            pass
        msg = "the corpus reader fell over"
        raise RuntimeError(msg)

    BACKGROUND_TASK_HANDLERS[kind] = _explode
    try:
        run_id = await _make_ticket(maker, committed_project, kind)
        with pytest.raises(RuntimeError):
            await background_task_job(
                _worker_ctx(maker, pool), str(run_id), _token(committed_project, run_id)
            )
        crashed = await _run(maker, run_id)
        assert crashed is not None
        assert crashed.status == "failed", "a raising handler left the ticket running"
        assert crashed.completed_at is not None
        assert "the corpus reader fell over" in (crashed.error_text or "")
        assert "background_task.failed" in await _ledger_kinds(maker, run_id)
    finally:
        BACKGROUND_TASK_HANDLERS.pop(kind, None)

    # (b) arq re-delivers a job whose worker was killed mid-flight.
    redelivered = await _make_ticket(maker, committed_project, "reindex_corpus")
    async with maker() as session:
        row = (
            await session.execute(select(AgentRun).where(AgentRun.id == redelivered))
        ).scalar_one()
        row.status = STATUS_RUNNING
        row.started_at = utcnow()
        await session.commit()

    result = await background_task_job(
        _worker_ctx(maker, pool), str(redelivered), _token(committed_project, redelivered)
    )
    assert result["ok"] is False
    again = await _run(maker, redelivered)
    assert again is not None
    assert again.status == "failed"
    assert "not retried" in (again.error_text or "")
    assert pool.jobs == [], "a re-delivered job repeated the fan-out"


async def test_a_ticket_whose_token_expired_in_the_queue_still_ends(
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """A dispatch token is capped at one hour; a longer queue must not strand.

    `mint_agent_token` refuses a TTL over 3600s, so a ticket that waits longer
    than that arrives with a token the worker cannot verify. Verifying before
    claiming and then raising would leave the row at `pending` forever while arq
    retried against the same dead token — visible only as a stack trace in a
    worker log. The ticket has to end.
    """
    pool = _RecordingPool()
    run_id = await _make_ticket(maker, committed_project, "review_sweep")

    result = await background_task_job(_worker_ctx(maker, pool), str(run_id), "not-a-token")

    assert result["ok"] is False
    run = await _run(maker, run_id)
    assert run is not None
    assert run.status == "failed"
    assert "token" in (run.error_text or "").lower()
    assert "background_task.failed" in await _ledger_kinds(maker, run_id)


# ---------------------------------------------------------------------------
# The gates the first pass shipped without a test
# ---------------------------------------------------------------------------


async def test_a_viewer_may_not_start_a_task(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """Starting work costs money and moves the corpus; reading does not.

    `require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)` was
    on the route from the first commit and was unreachable from the tests,
    which all cached OWNER. Deleting the line left every one of them green.
    """
    pool = _RecordingPool()
    app = _build_app_as(monkeypatch, maker, _principal(), pool, ProjectRole.VIEWER)
    async with await _client(app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={"kind": "reindex_corpus", "params": {}},
        )
    assert resp.status_code == 403, resp.text
    assert pool.jobs == [], "a refused request still put work on the bus"


async def test_a_viewer_may_not_cancel_a_task(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """Cancelling is a write too — it is how someone else's sweep gets stopped."""
    pool = _RecordingPool()
    owner_app = _build_app_as(monkeypatch, maker, _principal(), pool, ProjectRole.OWNER)
    async with await _client(owner_app) as client:
        started = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={"kind": "reindex_corpus", "params": {}},
        )
    assert started.status_code == 202, started.text
    run_id = started.json()["agent_run_id"]

    viewer_app = _build_app_as(monkeypatch, maker, _principal(), pool, ProjectRole.VIEWER)
    async with await _client(viewer_app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks/{run_id}/cancel"
        )
    assert resp.status_code == 403, resp.text

    run = await _run(maker, uuid.UUID(run_id))
    assert run is not None
    assert run.status != STATUS_CANCELLED, "a refused cancel cancelled the task anyway"


async def test_a_parent_run_in_another_project_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
) -> None:
    """`parent_agent_run_id` may not name a run in a different project.

    `agent_events` carries no `project_id` and no foreign key on
    `agent_run_id`, so the dispatch row was written against whatever UUID the
    request body named. A caller with EDITOR on project A could post a run id
    owned by project B and have a `background_task_dispatched` row appear in
    B's timeline naming A's child run — reachable from a prompt, because the
    parent id travels from the agent's own config.

    Proven before the fix: 202, and the row landed in B.
    """
    pool = _RecordingPool()
    # A chat run owned by the OTHER project.
    foreign_run = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=foreign_run,
                project_id=second_project,
                agent_kind="assistant",
                correlation_id=f"chat-{foreign_run.hex}",
                status="running",
                input_payload={},
                created_by=ACTOR,
            )
        )
        await session.commit()

    app = _build_app(monkeypatch, maker, _principal(), pool)
    async with await _client(app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={
                "kind": "reindex_corpus",
                "params": {},
                "parent_agent_run_id": str(foreign_run),
            },
        )

    assert resp.status_code == 422, resp.text
    assert pool.jobs == [], "a refused request still put work on the bus"

    async with maker() as session:
        leaked = (
            await session.execute(
                select(func.count())
                .select_from(AgentEvent)
                .where(AgentEvent.agent_run_id == foreign_run)
            )
        ).scalar_one()
    assert leaked == 0, "a dispatch event was written into another project's timeline"


async def test_a_parent_run_that_does_not_exist_is_refused_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """Same answer for "no such run" as for "a run in another project".

    Two different answers would turn this field into an oracle for whether a
    given run id exists somewhere in the deployment.
    """
    pool = _RecordingPool()
    app = _build_app(monkeypatch, maker, _principal(), pool)
    async with await _client(app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={
                "kind": "reindex_corpus",
                "params": {},
                "parent_agent_run_id": str(uuid7()),
            },
        )
    assert resp.status_code == 422, resp.text


async def test_a_parent_run_in_this_project_still_works(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
) -> None:
    """The refusal above must not have closed the feature it guards.

    Without this, deleting the whole `if parent_agent_run_id is not None:`
    block would pass the three tests above.
    """
    pool = _RecordingPool()
    parent = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=parent,
                project_id=committed_project,
                agent_kind="assistant",
                correlation_id=f"chat-{parent.hex}",
                status="running",
                input_payload={},
                created_by=ACTOR,
            )
        )
        await session.commit()

    app = _build_app(monkeypatch, maker, _principal(), pool)
    async with await _client(app) as client:
        resp = await client.post(
            f"/v1/projects/{committed_project}/background-tasks",
            json={
                "kind": "reindex_corpus",
                "params": {},
                "parent_agent_run_id": str(parent),
            },
        )
    assert resp.status_code == 202, resp.text

    async with maker() as session:
        events = list(
            (await session.execute(select(AgentEvent).where(AgentEvent.agent_run_id == parent)))
            .scalars()
            .all()
        )
    assert len(events) == 1, "the hand-off was not recorded on the parent turn"
    assert events[0].payload_jsonb["child_agent_run_id"] == resp.json()["agent_run_id"]
