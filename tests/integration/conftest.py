"""Fixtures for tests that need a real Postgres.

Everything here is marked `integration` and is skipped by the default
`pytest -m "not integration"` run. The database is the point: these cover
behaviour that lives in Postgres — triggers, constraints, transaction semantics
— and that a mocked session would report as passing while the real thing failed.

Requires `DATABASE_URL` (asyncpg) with migrations already applied:

    docker compose -f deploy/compose/docker-compose.yml up -d --wait postgres
    cd apps/api && uv run alembic upgrade head
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    """A fresh engine and session per test.

    Per-test rather than session-scoped on purpose: a session-scoped async
    engine binds its asyncpg pool to the first test's event loop, and every
    later test fails with "attached to a different loop".
    """
    engine = create_async_engine(database_url, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
    finally:
        await engine.dispose()
