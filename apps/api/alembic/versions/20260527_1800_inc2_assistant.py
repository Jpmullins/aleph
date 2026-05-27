"""inc2: assistant sessions/threads/messages.

Revision ID: inc2_assistant
Revises: inc1_rks_wiki
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc2_assistant"
down_revision: str | None = "inc1_rks_wiki"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common(*extra: sa.Column) -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "access_scope",
            sa.String(64),
            nullable=False,
            server_default="project",
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        *extra,
    ]


def upgrade() -> None:
    op.create_table(
        "assistant_sessions",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        ),
    )
    op.create_table(
        "assistant_threads",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("parent_thread_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("title", sa.String(255), nullable=True),
        ),
    )
    op.create_table(
        "assistant_messages",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="complete",
            ),
            sa.Column(
                "retrieval_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "attached_cards_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
            sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "cost_usd",
                sa.Numeric(12, 6),
                nullable=False,
                server_default="0",
            ),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("error_text", sa.String(4096), nullable=True),
        ),
        sa.UniqueConstraint("thread_id", "ordinal", name="uq_messages_thread_ord"),
    )


def downgrade() -> None:
    op.drop_table("assistant_messages")
    op.drop_table("assistant_threads")
    op.drop_table("assistant_sessions")
