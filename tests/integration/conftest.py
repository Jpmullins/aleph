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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"

# The teardown lives in a module, not here, so its completeness can be tested.
# See tests/integration/teardown.py and test_teardown_is_complete.py.
from tests.integration.teardown import teardown_project  # noqa: E402


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
async def second_project(
    engine: AsyncEngine, maker: Callable[[], AsyncSession]
) -> AsyncIterator[uuid.UUID]:
    """A SECOND project, for the tests that check one cannot reach the other.

    A cross-project test needs two real ids with independent teardown; reusing
    `committed_project` and inventing the other id tests nothing, because the
    invented one has no rows to leak into.
    """
    project_id = uuid.uuid4()
    yield project_id
    await teardown_project(engine, maker, project_id)


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
    await teardown_project(engine, maker, project_id)
