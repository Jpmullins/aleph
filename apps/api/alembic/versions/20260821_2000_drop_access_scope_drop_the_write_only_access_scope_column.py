"""drop the write-only access_scope column

`access_scope` was on every table, meant to record who a row is visible to.
It had **70 sites that wrote a value and zero that ever read one** — no query
in the codebase filtered on it. It looked like an access control and was not
one; the real control is the role check on each API route
(`require_at_least`, ~20 call sites).

An authorization concept written everywhere and enforced nowhere is worse than
having none, because it reads as protection that is not there. See
`docs/decisions.md` D7.

Forward: drop the column from every table that has it, discovered from the
catalog rather than hardcoded, so a table added since cannot be missed.
Backward: restore it with its original default, which is lossless — every row
carried the server default or the string 'project', and nothing read it.

Revision ID: drop_access_scope
Revises: ee0b492b2778
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "drop_access_scope"
down_revision: str | None = "ee0b492b2778"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables_with_column(conn: sa.Connection, column: str) -> list[str]:
    rows = conn.execute(
        sa.text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = :col "
            "ORDER BY table_name"
        ),
        {"col": column},
    )
    return [r[0] for r in rows]


def upgrade() -> None:
    conn = op.get_bind()
    for table in _tables_with_column(conn, "access_scope"):
        op.drop_column(table, "access_scope")


def downgrade() -> None:
    """Restore the column wherever a project_id lives.

    Not an exact inverse — it re-adds the column to every project-scoped table
    rather than to precisely the set that had it. That is deliberate: nothing
    ever read the column, so the only property worth preserving is that the
    schema accepts the old writes again.
    """
    conn = op.get_bind()
    for table in _tables_with_column(conn, "project_id"):
        op.add_column(
            table,
            sa.Column(
                "access_scope",
                sa.String(length=64),
                nullable=False,
                server_default="project",
            ),
        )
