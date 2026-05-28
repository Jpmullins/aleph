"""inc8: eval suite + UserFeedback.

Revision ID: inc8_evals_feedback
Revises: inc7_artifacts
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc8_evals_feedback"
down_revision: str | None = "inc7_artifacts"
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
    # eval_datasets
    op.create_table(
        "eval_datasets",
        *_common(
            sa.Column("name", sa.String(255), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fixture_path", sa.String(512), nullable=False),
            sa.Column("gate_kind", sa.String(32), nullable=False),
            sa.Column(
                "gate_thresholds_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column("introduced_in_increment", sa.Integer(), nullable=False),
        ),
    )

    # eval_cases
    op.create_table(
        "eval_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("eval_dataset_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("case_key", sa.String(255), nullable=False),
        sa.Column("payload_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column("expected_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column(
            "tags_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "origin",
            sa.String(32),
            nullable=False,
            server_default="fixture",
        ),
        sa.Column("origin_ref_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint(
            "eval_dataset_id", "case_key", name="uq_eval_cases_dataset_key"
        ),
    )

    # eval_runs
    op.create_table(
        "eval_runs",
        *_common(
            sa.Column("eval_dataset_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("model_profile_name", sa.String(64), nullable=False),
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("runner_version", sa.String(64), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="running",
            ),
            sa.Column(
                "pass_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "fail_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "metrics_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "cost_usd",
                sa.Numeric(12, 4),
                nullable=False,
                server_default="0",
            ),
            sa.Column("report_uri", sa.String(1024), nullable=True),
        ),
    )

    # eval_results
    op.create_table(
        "eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("eval_run_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("eval_case_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("actual_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column(
            "diff_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "latency_ms", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
    )

    # user_feedback
    op.create_table(
        "user_feedback",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("target_kind", sa.String(32), nullable=False),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("signal", sa.String(32), nullable=False),
            sa.Column(
                "note", sa.Text(), nullable=False, server_default=""
            ),
            sa.Column("severity", sa.String(16), nullable=True),
            sa.Column(
                "context_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "promoted_to_eval_case_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
        ),
    )


def downgrade() -> None:
    op.drop_table("user_feedback")
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
    op.drop_table("eval_cases")
    op.drop_table("eval_datasets")
