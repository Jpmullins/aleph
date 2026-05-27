"""Async engine + sessionmaker factory.

Engines are bound to a `database_url` (sync SQLAlchemy URL with the
`+asyncpg` driver). Callers obtain a session via dependency injection in
the API layer or directly in workers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def async_engine_for(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create a new async engine. Caller owns disposal."""
    return create_async_engine(
        database_url,
        echo=echo,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        future=True,
    )


def async_sessionmaker_for(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def get_session(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a session in a context manager. Commits on success; rolls back on
    exception. The API dependency `session_dep` wraps this for request scope."""
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
