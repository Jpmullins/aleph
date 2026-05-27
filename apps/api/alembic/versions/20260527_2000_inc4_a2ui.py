"""inc4: A2UI cards + Notes.

Revision ID: inc4_a2ui
Revises: inc3_aiq_synthesis
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc4_a2ui"
down_revision: str | None = "inc3_aiq_synthesis"
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
    # ---- interactive_cards ------------------------------------------------
    op.create_table(
        "interactive_cards",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("card_kind", sa.String(64), nullable=False),
            sa.Column("catalog_version", sa.String(32), nullable=False),
            sa.Column("title", sa.String(255), nullable=True),
            sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("pinned_to", sa.String(32), nullable=True),
            sa.Column("pinned_target_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
    )

    # ---- interactive_card_versions ----------------------------------------
    op.create_table(
        "interactive_card_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "a2ui_payload_jsonb", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "data_model_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("author_kind", sa.String(16), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint("card_id", "version_no", name="uq_card_version_no"),
    )
    # Immutability — same pattern as wiki_revisions.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION card_versions_immutable() RETURNS TRIGGER AS $$
        BEGIN
          RAISE EXCEPTION 'interactive_card_versions is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_versions_no_update BEFORE UPDATE
          ON interactive_card_versions
          FOR EACH ROW EXECUTE FUNCTION card_versions_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_versions_no_delete BEFORE DELETE
          ON interactive_card_versions
          FOR EACH ROW EXECUTE FUNCTION card_versions_immutable();
        """
    )

    # ---- card_actions -----------------------------------------------------
    op.create_table(
        "card_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("surface_kind", sa.String(32), nullable=False),
        sa.Column("action_kind", sa.String(64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_kind", sa.String(64), nullable=True),
        sa.Column(
            "params_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "result_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # ---- notes + note_sections --------------------------------------------
    op.create_table(
        "notes",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("title", sa.String(255), nullable=False),
        ),
    )
    op.create_table(
        "note_sections",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("note_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
            sa.Column("anchor", sa.String(255), nullable=True),
        ),
        sa.UniqueConstraint(
            "note_id", "ordinal", name="uq_note_sections_note_ord"
        ),
    )


def downgrade() -> None:
    op.drop_table("note_sections")
    op.drop_table("notes")
    op.drop_table("card_actions")
    op.execute(
        "DROP TRIGGER IF EXISTS card_versions_no_update ON interactive_card_versions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS card_versions_no_delete ON interactive_card_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS card_versions_immutable")
    op.drop_table("interactive_card_versions")
    op.drop_table("interactive_cards")
