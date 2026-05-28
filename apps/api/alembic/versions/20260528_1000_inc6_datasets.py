"""inc6: datasets + dataset_versions + observations.

Revision ID: inc6_datasets
Revises: inc5_reviewers_hypotheses
Create Date: 2026-05-28
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc6_datasets"
down_revision: str | None = "inc5_reviewers_hypotheses"
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
    # ---- datasets ---------------------------------------------------------
    op.create_table(
        "datasets",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "description", sa.Text(), nullable=False, server_default=""
            ),
            sa.Column("dataset_kind", sa.String(32), nullable=False),
            sa.Column("source_connector_kind", sa.String(64), nullable=True),
            sa.Column("short_id", sa.String(16), nullable=False, unique=True),
            sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "column_schema_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
        ),
    )

    # ---- dataset_versions -------------------------------------------------
    op.create_table(
        "dataset_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("column_schema_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column("parquet_uri", sa.String(1024), nullable=True),
        sa.Column(
            "rows_inline",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("data_sha256", sa.String(64), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "diff_summary_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
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
        sa.UniqueConstraint(
            "dataset_id", "version_no", name="uq_dataset_version_no"
        ),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dataset_versions_immutable() RETURNS TRIGGER AS $$
        BEGIN
          RAISE EXCEPTION 'dataset_versions is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER dataset_versions_no_update BEFORE UPDATE
          ON dataset_versions
          FOR EACH ROW EXECUTE FUNCTION dataset_versions_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER dataset_versions_no_delete BEFORE DELETE
          ON dataset_versions
          FOR EACH ROW EXECUTE FUNCTION dataset_versions_immutable();
        """
    )

    # ---- observations -----------------------------------------------------
    op.create_table(
        "observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("dataset_version_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("payload_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column(
            "source_refs_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )

    # ---- seed artificialanalysis connector --------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO connectors (
              id, created_by, access_scope, kind, name, output_kind,
              requires_auth, metadata_schema_jsonb, enabled_by_default
            ) VALUES (
              CAST(:id AS uuid), CAST(:sys AS uuid), 'global', 'artificialanalysis', 'artificialanalysis.ai',
              'dataset_rows', true, CAST(:schema AS jsonb), true
            )
            ON CONFLICT (kind) DO NOTHING
            """
        ).bindparams(
            sa.bindparam("id", str(uuid4())),
            sa.bindparam("sys", str(uuid4())),
            sa.bindparam(
                "schema",
                json.dumps({"$schema": "http://json-schema.org/draft-07/schema#"}),
            ),
        )
    )


def downgrade() -> None:
    op.execute("DELETE FROM connectors WHERE kind = 'artificialanalysis'")
    op.drop_table("observations")
    op.execute("DROP TRIGGER IF EXISTS dataset_versions_no_update ON dataset_versions")
    op.execute("DROP TRIGGER IF EXISTS dataset_versions_no_delete ON dataset_versions")
    op.execute("DROP FUNCTION IF EXISTS dataset_versions_immutable")
    op.drop_table("dataset_versions")
    op.drop_table("datasets")
