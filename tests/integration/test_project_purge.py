"""Deleting a project must eventually delete its rows.

Deleting a project is a PATCH to `status = "deleted"` — a soft delete, and that
is correct: someone who deletes one by mistake should get it back. Nothing ever
purged the rows behind one, so a deployment accumulated every project it had
ever held.

Measured 2026-08-29 on this instance before the purge existed: 1,136 projects,
ONE with content anyone wanted, and 1.6 million rows behind the rest — 336k wiki
links, 265k agent events, 176k wiki sections, 55k document chunks. The e2e suite
is the biggest single contributor and it is not misbehaving: every spec that
creates a project deletes it, and the delete is a soft one.

The tests that matter here are the two REFUSALS — a live project is untouched,
and the append-only tables survive — because a purge that takes too much is a
far worse bug than one that takes too little.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentRun
from aleph_db.purge import PURGE_EXEMPT, purge_project_rows, purgeable_tables

pytestmark = pytest.mark.integration


async def _seed_run(maker, project_id: uuid.UUID) -> uuid.UUID:
    run = AgentRun(
        id=uuid7(),
        project_id=project_id,
        agent_kind="purge-probe",
        correlation_id=str(uuid7()),
        status="succeeded",
        input_payload={},
        created_by=uuid.uuid4(),
    )
    async with maker() as s:
        s.add(run)
        await s.commit()
    return run.id


async def test_a_purge_removes_the_rows_behind_a_project(
    maker, committed_project: uuid.UUID
) -> None:
    run_id = await _seed_run(maker, committed_project)

    async with maker() as s:
        removed = await purge_project_rows(s, project_id=committed_project)
        await s.commit()

    assert removed.get("agent_runs", 0) >= 1
    async with maker() as s:
        gone = (
            await s.execute(text("SELECT count(*) FROM agent_runs WHERE id = :i"), {"i": run_id})
        ).scalar_one()
    assert gone == 0


async def test_a_purge_does_not_touch_another_project(
    maker, committed_project: uuid.UUID, second_project: uuid.UUID
) -> None:
    """The refusal that matters most. A purge scoped wrongly is unrecoverable."""
    keep = await _seed_run(maker, second_project)
    await _seed_run(maker, committed_project)

    async with maker() as s:
        await purge_project_rows(s, project_id=committed_project)
        await s.commit()

    async with maker() as s:
        survived = (
            await s.execute(text("SELECT count(*) FROM agent_runs WHERE id = :i"), {"i": keep})
        ).scalar_one()
    assert survived == 1, "a purge reached into another project"


async def test_the_append_only_tables_are_never_purged(maker) -> None:
    """A scheduled job must not disable a database trigger to tidy up.

    An operator clearing a machine may reasonably bypass them by hand, once,
    with a backup. A job doing it on a timer has turned an invariant into a
    suggestion — and the ledger is what makes "who did what" answerable at all.
    """
    async with maker() as s:
        targets = set(await purgeable_tables(s))

        trigger_rows = (
            await s.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgname LIKE '%no_delete%'"
                )
            )
        ).scalars().all()

    append_only = {str(r) for r in trigger_rows}
    assert append_only, "found no append-only triggers — wrong database?"
    collide = sorted(targets & append_only)
    assert not collide, (
        f"the purge would DELETE from append-only table(s) {collide}; it would "
        f"raise, and a job that disabled the trigger instead would make the "
        f"ledger untrustworthy"
    )
    for table in append_only:
        assert table in PURGE_EXEMPT, f"{table} is append-only but not documented as exempt"


async def test_every_exemption_names_a_real_table_and_a_reason(maker) -> None:
    """A stale exemption is a table leaking under an excuse nobody rechecked."""
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE c.relkind='r' AND n.nspname='public'"
                )
            )
        ).scalars().all()
    existing = {str(r) for r in rows}
    for table, reason in PURGE_EXEMPT.items():
        assert table in existing, f"exempt table {table} does not exist"
        assert reason.strip(), f"{table} is exempt with no reason"


async def test_the_kind_and_its_handler_agree(maker) -> None:
    """A kind the route accepts with no handler is a ticket that can only fail."""
    from aleph_db.repos.background_tasks import BACKGROUND_TASK_KINDS
    from aleph_workers.jobs.background_kinds import BACKGROUND_TASK_HANDLERS

    assert "purge_deleted_projects" in BACKGROUND_TASK_KINDS
    assert set(BACKGROUND_TASK_KINDS) == set(BACKGROUND_TASK_HANDLERS)
