"""Alembic environment. Async engine, reads URL from DATABASE_URL env."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import TYPE_CHECKING, Literal

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from sqlalchemy.sql.schema import SchemaItem

# Import every model package so Base.metadata is populated.
import aleph_db.models  # noqa: F401  (registers tables with metadata)
from aleph_db.base import Base

# Inc 1: register the RKS and Wiki model trees.
try:
    import aleph_rks.models  # noqa: F401
except ImportError:
    pass
try:
    import aleph_wiki.models  # noqa: F401
except ImportError:
    pass
# Inc 2: assistant.
try:
    import aleph_assistant.models  # noqa: F401
except ImportError:
    pass
# Inc 3: connectors + AIQ.
try:
    import aleph_connectors.credentials
    import aleph_connectors.models  # noqa: F401
except ImportError:
    pass
# Inc 4: A2UI + notes.
try:
    import aleph_a2ui.models  # noqa: F401
    import aleph_notes.models  # noqa: F401
except ImportError:
    pass
# Inc 5: reviewer + hypotheses + agent memory.
try:
    import aleph_core.agent_memory
    import aleph_hypotheses.models  # noqa: F401
    import aleph_reviewer.models  # noqa: F401
except ImportError:
    pass
# Inc 6: datasets.
try:
    import aleph_datasets.models  # noqa: F401
except ImportError:
    pass
# Inc 7: artifacts.
try:
    import aleph_artifacts.models  # noqa: F401
except ImportError:
    pass
# Inc 8: evals + user feedback.
try:
    import aleph_core.feedback  # noqa: F401
    import aleph_evals.models  # noqa: F401
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

# Tables created at runtime by langgraph's AsyncPostgresStore.setup() (Wave 6
# cross-session memory). They are not Aleph ORM models, so they are absent from
# Base.metadata; without this guard autogenerate would propose dropping them.
_IGNORED_TABLES = {"store", "store_migrations"}

# Expression / partial indexes that alembic cannot reflect-compare and would
# therefore re-flag on every run. `uq_chain_head_global` is a partial unique
# index on the expression `((1)) WHERE project_id IS NULL`, created by raw SQL
# in the initial migration; it has no ORM-metadata counterpart.
_IGNORED_INDEXES = {"uq_chain_head_global"}


def include_name(
    name: str | None,
    type_: str,
    parent_names: MutableMapping[
        Literal["schema_name", "table_name", "schema_qualified_table_name"],
        str | None,
    ],
) -> bool:
    return not (type_ == "table" and name in _IGNORED_TABLES)


def include_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    return not (type_ == "index" and name in _IGNORED_INDEXES)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_name=include_name,
        include_object=include_object,
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
