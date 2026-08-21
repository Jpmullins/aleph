"""A run whose owning process died must stop claiming to be running.

An `AgentRun` is set to `running` by the process doing the work and moved to a
terminal status by that same process. Nothing else touches it. So a worker
restart, an OOM kill, or an exception on a path with no `finally` leaves the row
`running` forever — and the UI shows work in progress that no longer exists.

The deployed stack carried 45 `chunk_embed` runs in that state, every one a
failed index nobody had been told about. Forty-five identical silent failures is
what an unreconciled status column looks like.

These pin the reconciliation, and — more importantly — pin that it does not reap
runs that are simply young. A reaper that is too eager kills live work, which is
strictly worse than the problem it solves.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.repos.agent_runs import reap_stale_runs, stale_running_runs

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _run(
    project_id: uuid.UUID,
    *,
    status: str,
    age: timedelta | None,
    kind: str = "chunk_embed",
) -> AgentRun:
    return AgentRun(
        id=uuid.uuid4(),
        project_id=project_id,
        agent_kind=kind,
        correlation_id=f"t-{uuid.uuid4().hex}",
        status=status,
        started_at=None if age is None else utcnow() - age,
        input_payload={},
        created_by=ACTOR,
    )


async def test_a_run_running_past_the_deadline_is_failed(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    async with maker() as s:
        stale = _run(committed_project, status="running", age=timedelta(hours=3))
        s.add(stale)
        await s.commit()
        stale_id = stale.id

    async with maker() as s:
        reaped = await reap_stale_runs(s)
        await s.commit()
    assert reaped >= 1

    async with maker() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == stale_id))).scalar_one()
    assert row.status == "failed"
    assert row.completed_at is not None
    assert row.error_text and "reaped" in row.error_text


async def test_a_young_run_is_left_alone(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The failure mode of an over-eager reaper is killing live work."""
    async with maker() as s:
        young = _run(committed_project, status="running", age=timedelta(minutes=2))
        s.add(young)
        await s.commit()
        young_id = young.id

    async with maker() as s:
        await reap_stale_runs(s)
        await s.commit()

    async with maker() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == young_id))).scalar_one()
    assert row.status == "running", "a two-minute-old run is not a dead one"


async def test_a_run_with_no_start_time_is_left_alone(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`started_at IS NULL` is work that never began, not work that never ended."""
    async with maker() as s:
        never = _run(committed_project, status="running", age=None)
        s.add(never)
        await s.commit()
        never_id = never.id

    async with maker() as s:
        selected = await stale_running_runs(s)
        assert never_id not in {r.id for r in selected}
        await reap_stale_runs(s)
        await s.commit()

    async with maker() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == never_id))).scalar_one()
    assert row.status == "running"


async def test_a_terminal_run_is_not_touched(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    async with maker() as s:
        done = _run(committed_project, status="succeeded", age=timedelta(days=2))
        s.add(done)
        await s.commit()
        done_id = done.id

    async with maker() as s:
        await reap_stale_runs(s)
        await s.commit()

    async with maker() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == done_id))).scalar_one()
    assert row.status == "succeeded"
    assert row.error_text is None


async def test_every_reap_is_ledgered_in_the_same_transaction(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A state mutation nobody can audit is how the original silence happened."""
    async with maker() as s:
        for _ in range(3):
            s.add(_run(committed_project, status="running", age=timedelta(hours=4)))
        await s.commit()

    async with maker() as s:
        reaped = await reap_stale_runs(s)
        await s.commit()
    assert reaped >= 3

    async with maker() as s:
        events = list(
            (
                await s.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == "agent_run.reaped",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) >= 3
    assert all(e.payload_jsonb.get("reason") for e in events)


async def test_a_rollback_leaves_neither_the_status_nor_the_event(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Same transaction means same transaction — provable only by rolling back."""
    async with maker() as s:
        run = _run(committed_project, status="running", age=timedelta(hours=5))
        s.add(run)
        await s.commit()
        run_id = run.id

    async with maker() as s:
        await reap_stale_runs(s)
        await s.rollback()

    async with maker() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
        events = list(
            (
                await s.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == "agent_run.reaped",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert row.status == "running"
    assert not events


async def test_reaping_twice_finds_nothing_the_second_time(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    async with maker() as s:
        s.add(_run(committed_project, status="running", age=timedelta(hours=6)))
        await s.commit()

    async with maker() as s:
        first = await reap_stale_runs(s)
        await s.commit()
    async with maker() as s:
        second = await reap_stale_runs(s)
        await s.commit()

    assert first >= 1
    assert second == 0, "boot runs this every time; it must be a no-op when there is nothing to do"
