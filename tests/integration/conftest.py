"""Fixtures for tests that need a real Postgres.

Everything here is marked `integration` and is skipped by the default
`pytest -m "not integration"` run. The database is the point: these cover
behaviour that lives in Postgres — triggers, constraints, transaction semantics
— and that a mocked session would report as passing while the real thing failed.

Two shapes of fixture, and the difference matters:

* ``session`` — one session, nothing committed, rolled back at the end. Fast,
  and correct for anything that reads and writes through the session it was
  given.
* ``maker`` + ``committed_project`` — a session *factory* and a project whose
  rows are really committed, torn down explicitly afterwards. Required whenever
  the code under test opens its own sessions, which is exactly the code where
  transaction boundaries are the behaviour being tested (indexing commits its
  chunks before it embeds them, so a test that shared one transaction could not
  observe the property it exists to check).

Requires `DATABASE_URL` (asyncpg) with migrations already applied:

    docker compose -f deploy/compose/docker-compose.yml up -d --wait postgres
    cd apps/api && uv run alembic upgrade head
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"

#: How a committed integration fixture is torn down, in delete order. Each entry
#: is a statement scoped to one project id — named explicitly rather than
#: reflected, because a truncate-everything teardown is a data-loss bug waiting
#: for the first person who points DATABASE_URL at the running compose Postgres,
#: which is the documented way to run these.
_TEARDOWN_SQL = (
    # The wiki tables, in FK order, and they were ALL missing.
    #
    # `DELETE FROM projects` ran while every wiki row for that project stayed
    # behind, orphaned — pointing at a project id that no longer resolves. That
    # is not merely untidy: `test_stub_pages_are_not_drafts` counts
    # `is_stub AND status='draft'` across the whole database as a canary for the
    # deployed instance, and it went red on 20 rows titled "Gamma" left by a
    # concurrency fixture whose project had been deleted three hours earlier.
    # The invariant it guards is real and the code upholds it; the rows were
    # rubbish this teardown should never have left.
    "DELETE FROM citations WHERE project_id = :pid",
    "DELETE FROM claim_edges WHERE project_id = :pid",
    "DELETE FROM wiki_claims WHERE project_id = :pid",
    "DELETE FROM wiki_sections WHERE project_id = :pid",
    "DELETE FROM wiki_links WHERE project_id = :pid",
    "DELETE FROM wiki_index WHERE project_id = :pid",
    "DELETE FROM synthesis_proposals WHERE project_id = :pid",
    "DELETE FROM wiki_schemas WHERE project_id = :pid",
    # `wiki_pages` and `wiki_revisions` are deliberately NOT deleted.
    #
    # `wiki_revisions` is append-only, enforced by a database trigger
    # (`wiki_revisions_immutable`) — the same protection the action ledger has,
    # and for the same reason. A DELETE raises. `wiki_pages` cannot go either:
    # revisions carry a FK back to their page, so removing the page would
    # violate it.
    #
    # So a test that commits a revision leaves a permanent page row behind, and
    # that is a property of the design rather than a leak to plug. A fixture
    # that switched the trigger off to tidy up would be trading an invariant for
    # a clean table, which is how the invariant stops being one.
    #
    # The consequence lands on any check that counts wiki_pages globally: see
    # `test_stub_pages_are_not_drafts`, which scopes to LIVE projects for
    # exactly this reason.
    "DELETE FROM document_chunks WHERE project_id = :pid",
    "DELETE FROM retrieval_index_records WHERE project_id = :pid",
    "DELETE FROM normalized_documents WHERE project_id = :pid",
    "DELETE FROM source_versions WHERE source_id IN"
    " (SELECT id FROM sources WHERE project_id = :pid)",
    "DELETE FROM source_assets WHERE project_id = :pid",
    "DELETE FROM sources WHERE project_id = :pid",
    "DELETE FROM agent_events WHERE agent_run_id IN"
    " (SELECT id FROM agent_runs WHERE project_id = :pid)",
    "DELETE FROM model_calls WHERE project_id = :pid",
    "DELETE FROM cost_ledger_events WHERE project_id = :pid",
    "DELETE FROM agent_runs WHERE project_id = :pid",
    # `action_ledger_events` and its head are deliberately NOT deleted. The
    # table carries an append-only trigger, and `aleph` is a superuser in the
    # compose stack — so a teardown *could* bypass it with
    # `session_replication_role`. It must not: a fixture that switches off a
    # core invariant to tidy up is how the invariant stops being one. The rows
    # are scoped to a throwaway project id and interfere with nothing.
    "DELETE FROM model_profiles WHERE project_id = :pid",
    "DELETE FROM projects WHERE id = :pid",
)


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Per-test rather than session-scoped on purpose: a session-scoped async
    engine binds its asyncpg pool to the first test's event loop, and every
    later test fails with "attached to a different loop".
    """
    eng = create_async_engine(database_url, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One session, uncommitted, rolled back at the end."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.fixture
def maker(engine: AsyncEngine) -> Callable[[], AsyncSession]:
    """A session factory, for code that opens its own transactions."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def committed_project(
    engine: AsyncEngine, maker: Callable[[], AsyncSession]
) -> AsyncIterator[uuid.UUID]:
    """A project id whose rows are really committed, deleted afterwards.

    The teardown is scoped to this project id on every table, so it cannot
    touch a real corpus in the same database.
    """
    project_id = uuid.uuid4()
    yield project_id
    async with maker() as s:
        for statement in _TEARDOWN_SQL:
            await s.execute(text(statement), {"pid": project_id})
        await s.commit()
