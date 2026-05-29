"""inc7: artifacts + rendered_assets.

Revision ID: inc7_artifacts
Revises: inc6_datasets
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc7_artifacts"
down_revision: str | None = "inc6_datasets"
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
    # rendered_assets
    op.create_table(
        "rendered_assets",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("source_kind", sa.String(32), nullable=False),
            sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "dataset_version_ids",
                postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
            sa.Column("output_format", sa.String(16), nullable=False),
            sa.Column("storage_uri", sa.String(1024), nullable=False),
            sa.Column("width_px", sa.Integer(), nullable=True),
            sa.Column("height_px", sa.Integer(), nullable=True),
            sa.Column("bytes_size", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("render_spec_jsonb", postgresql.JSONB(), nullable=False),
        ),
    )

    # artifacts
    op.create_table(
        "artifacts",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("short_id", sa.String(16), nullable=False, unique=True),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("artifact_kind", sa.String(32), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
    )

    # artifact_versions
    op.create_table(
        "artifact_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("storage_uri", sa.String(1024), nullable=False),
        sa.Column("bytes_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lineage_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column("template_name", sa.String(64), nullable=False),
        sa.Column("csl_style", sa.String(64), nullable=False),
        sa.Column("builder_agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("author_kind", sa.String(16), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint("artifact_id", "version_no", name="uq_artifact_version_no"),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION artifact_versions_immutable() RETURNS TRIGGER AS $$
        BEGIN
          RAISE EXCEPTION 'artifact_versions is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER artifact_versions_no_update BEFORE UPDATE
          ON artifact_versions
          FOR EACH ROW EXECUTE FUNCTION artifact_versions_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER artifact_versions_no_delete BEFORE DELETE
          ON artifact_versions
          FOR EACH ROW EXECUTE FUNCTION artifact_versions_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS artifact_versions_no_update ON artifact_versions")
    op.execute("DROP TRIGGER IF EXISTS artifact_versions_no_delete ON artifact_versions")
    op.execute("DROP FUNCTION IF EXISTS artifact_versions_immutable")
    op.drop_table("artifact_versions")
    op.drop_table("artifacts")
    op.drop_table("rendered_assets")
