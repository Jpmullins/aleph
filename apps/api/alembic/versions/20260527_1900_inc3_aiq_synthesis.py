"""inc3: AIQ + synthesis — ConnectorCredential, SynthesisProposal, ApprovalDecision.

Revision ID: inc3_aiq_synthesis
Revises: inc2_assistant
Create Date: 2026-05-27
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc3_aiq_synthesis"
down_revision: str | None = "inc2_assistant"
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


_INC3_CONNECTORS = [
    # (kind, name, output_kind, requires_auth, enabled_by_default)
    ("tavily", "Tavily Web Search", "document", True, True),
    ("exa", "Exa Neural Web Search", "document", True, True),
    ("serper", "Serper Google Scholar", "document", True, True),
    ("arxiv", "arXiv", "document", False, True),
    ("semantic_scholar", "Semantic Scholar", "document", False, True),
    ("openalex", "OpenAlex", "document", False, True),
    ("rss", "RSS Feed", "document", False, True),
    ("huggingface_hub", "HuggingFace Hub", "document", False, True),
    # Lens.org disabled by default — credential pending.
    ("lens", "Lens.org", "document", True, False),
]


def upgrade() -> None:
    # ---- connector_credentials --------------------------------------------
    op.create_table(
        "connector_credentials",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("cipher_blob", sa.LargeBinary(), nullable=False),
            sa.Column("cipher_scheme", sa.String(32), nullable=False),
            sa.Column("kms_key_arn", sa.String(2048), nullable=True),
            sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        ),
        sa.UniqueConstraint("project_id", "connector_id", name="uq_cred_project_connector"),
    )

    # ---- synthesis_proposals ----------------------------------------------
    op.create_table(
        "synthesis_proposals",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("topic", sa.String(512), nullable=False),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("approval_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
        sa.UniqueConstraint("page_id", "revision_id", name="uq_synth_page_rev"),
    )

    # ---- approval_decisions -----------------------------------------------
    op.create_table(
        "approval_decisions",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("target_kind", sa.String(64), nullable=False),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("reason", sa.String(4096), nullable=True),
            sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        ),
    )

    # ---- seed Inc 3 connectors --------------------------------------------
    system_user_id = str(uuid4())
    for kind, name, output_kind, requires_auth, enabled_by_default in _INC3_CONNECTORS:
        op.execute(
            sa.text(
                """
                INSERT INTO connectors (
                  id, created_by, access_scope, kind, name, output_kind,
                  requires_auth, metadata_schema_jsonb, enabled_by_default
                ) VALUES (
                  CAST(:id AS uuid), CAST(:sys AS uuid), 'global', :kind, :name, :output_kind,
                  :req_auth, CAST(:schema AS jsonb), :enabled
                )
                ON CONFLICT (kind) DO NOTHING
                """
            ).bindparams(
                sa.bindparam("id", str(uuid4())),
                sa.bindparam("sys", system_user_id),
                sa.bindparam("kind", kind),
                sa.bindparam("name", name),
                sa.bindparam("output_kind", output_kind),
                sa.bindparam("req_auth", requires_auth),
                sa.bindparam(
                    "schema", json.dumps({"$schema": "http://json-schema.org/draft-07/schema#"})
                ),
                sa.bindparam("enabled", enabled_by_default),
            )
        )


def downgrade() -> None:
    op.drop_table("approval_decisions")
    op.drop_table("synthesis_proposals")
    op.drop_table("connector_credentials")
    for kind, *_ in _INC3_CONNECTORS:
        op.execute(
            sa.text("DELETE FROM connectors WHERE kind = :k").bindparams(sa.bindparam("k", kind))
        )
