"""Alembic environment. Async engine, reads URL from DATABASE_URL env."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import every model package so Base.metadata is populated.
from aleph_db.base import Base
import aleph_db.models  # noqa: F401  (registers tables with metadata)
# Inc 1: register the RKS and Wiki model trees.
try:
    import aleph_rks.models  # noqa: F401
except ImportError:
    pass
try:
    import aleph_wiki.models  # noqa: F401
except ImportError:
    pass

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Pull DATABASE_URL from env; the alembic.ini value is intentionally blank.
db_url = os.environ.get("DATABASE_URL")
if db_url is None:
    msg = "DATABASE_URL env var is required for alembic"
    raise RuntimeError(msg)
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
