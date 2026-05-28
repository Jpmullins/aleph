"""inc5: reviewers + hypotheses + AgentMemory.

Revision ID: inc5_reviewers_hypotheses
Revises: inc4_a2ui
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc5_reviewers_hypotheses"
down_revision: str | None = "inc4_a2ui"
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
    # ---- review_runs ------------------------------------------------------
    op.create_table(
        "review_runs",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("trigger", sa.String(32), nullable=False),
            sa.Column("target_revision_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
            sa.Column(
                "target_scope",
                sa.String(16),
                nullable=False,
                server_default="revision",
            ),
            sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="running",
            ),
            sa.Column(
                "finding_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )

    # ---- review_findings --------------------------------------------------
    op.create_table(
        "review_findings",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("review_run_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("finding_kind", sa.String(64), nullable=False),
            sa.Column("severity", sa.String(16), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("target_page_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
            sa.Column("target_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("target_section_anchor", sa.String(512), nullable=True),
            sa.Column("target_claim_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("target_source_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "evidence_refs_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
            sa.Column("proposed_patch_jsonb", postgresql.JSONB(), nullable=True),
            sa.Column(
                "auto_resolvable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="open",
            ),
            sa.Column("approval_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
    )

    # ---- approval_requests ------------------------------------------------
    op.create_table(
        "approval_requests",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("target_kind", sa.String(64), nullable=False),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column(
                "severity",
                sa.String(16),
                nullable=False,
                server_default="medium",
            ),
            sa.Column("proposed_patch_jsonb", postgresql.JSONB(), nullable=True),
            sa.Column(
                "evidence_refs_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
            sa.Column("requested_by_kind", sa.String(16), nullable=False),
            sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
    )

    # ---- hypotheses + versions + evidence ---------------------------------
    op.create_table(
        "hypotheses",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("short_id", sa.String(16), nullable=False, unique=True),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column(
                "confidence",
                sa.String(24),
                nullable=False,
                server_default="under_investigation",
            ),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="active",
            ),
            sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("last_evidence_change_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )
    op.create_table(
        "hypothesis_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(24), nullable=False),
        sa.Column(
            "rationale", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("author_kind", sa.String(16), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint(
            "hypothesis_id", "version_no", name="uq_hypothesis_version_no"
        ),
    )
    # Same immutability pattern as wiki_revisions.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION hypothesis_versions_immutable() RETURNS TRIGGER AS $$
        BEGIN
          RAISE EXCEPTION 'hypothesis_versions is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER hypothesis_versions_no_update BEFORE UPDATE
          ON hypothesis_versions
          FOR EACH ROW EXECUTE FUNCTION hypothesis_versions_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER hypothesis_versions_no_delete BEFORE DELETE
          ON hypothesis_versions
          FOR EACH ROW EXECUTE FUNCTION hypothesis_versions_immutable();
        """
    )

    op.create_table(
        "hypothesis_evidence",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("hypothesis_version_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("stance", sa.String(16), nullable=False),
            sa.Column("evidence_kind", sa.String(32), nullable=False),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "weight", sa.Float(), nullable=False, server_default="1.0"
            ),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
        ),
    )

    # ---- agent_memories ---------------------------------------------------
    op.create_table(
        "agent_memories",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("agent_kind", sa.String(64), nullable=False, index=True),
            sa.Column("namespace", sa.String(128), nullable=False),
            sa.Column("key", sa.String(255), nullable=False),
            sa.Column("value_jsonb", postgresql.JSONB(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        ),
        sa.UniqueConstraint(
            "project_id", "agent_kind", "namespace", "key",
            name="uq_agent_memory_lookup",
        ),
    )
    op.create_index(
        "ix_agent_memory_namespace",
        "agent_memories",
        ["project_id", "agent_kind", "namespace"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_memory_namespace", "agent_memories")
    op.drop_table("agent_memories")
    op.drop_table("hypothesis_evidence")
    op.execute("DROP TRIGGER IF EXISTS hypothesis_versions_no_update ON hypothesis_versions")
    op.execute("DROP TRIGGER IF EXISTS hypothesis_versions_no_delete ON hypothesis_versions")
    op.execute("DROP FUNCTION IF EXISTS hypothesis_versions_immutable")
    op.drop_table("hypothesis_versions")
    op.drop_table("hypotheses")
    op.drop_table("approval_requests")
    op.drop_table("review_findings")
    op.drop_table("review_runs")
