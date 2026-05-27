"""inc0: foundations — identity, project, ledger, agent, cost, model_profile.

Revision ID: inc0_initial
Revises:
Create Date: 2026-05-27
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc0_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Inline binding maps for seeded ModelProfile rows.
# Mirrors top-level spec §9.1 and Inc 0 spec §0.10.
# ---------------------------------------------------------------------------

_ALEPH_DEV_BINDINGS = {
    "synthesis": {
        "model": "claude-sonnet-4-6",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "judge": {
        "model": "claude-sonnet-4-6",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "page_selection": {
        "model": "claude-haiku-4-5",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "extraction": {
        "model": "claude-haiku-4-5",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "vision": {
        "model": "claude-haiku-4-5",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "classification": {
        "model": "claude-haiku-4-5",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "embedding": {
        "model": "cohere-embed-english-v3",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 8192,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
}

_ALEPH_PROD_BINDINGS = {
    "synthesis": {
        "model": "claude-opus-4-7",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "judge": {
        "model": "claude-opus-4-7",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "page_selection": {
        "model": "claude-sonnet-4-6",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "extraction": {
        "model": "claude-sonnet-4-6",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "vision": {
        "model": "claude-sonnet-4-6",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "classification": {
        "model": "claude-haiku-4-5",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 200000,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
    "embedding": {
        "model": "cohere-embed-v4",
        "provider": "litellm",
        "fallback": None,
        "max_input_tokens": 8192,
        "cost_per_input_token_usd": "0",
        "cost_per_output_token_usd": "0",
        "cache_discount_pct": "0",
    },
}


def upgrade() -> None:
    # ---- extensions ----
    op.execute("CREATE EXTENSION IF NOT EXISTS pgvector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ---- users ----
    op.create_table(
        "users",
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
            server_default="global",
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subject", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # ---- projects ----
    op.create_table(
        "projects",
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
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.String(4096), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "model_profile_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("budget_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # ---- project_members ----
    op.create_table(
        "project_members",
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
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), nullable=False, index=True
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "project_id", "user_id", name="uq_project_members_proj_user"
        ),
    )

    # ---- model_profiles ----
    op.create_table(
        "model_profiles",
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
            server_default="global",
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=True, index=True
        ),
        sa.Column(
            "is_template",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("bindings_jsonb", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "project_id", "name", name="uq_profile_project_name"
        ),
    )

    # ---- budgets ----
    op.create_table(
        "budgets",
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
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
            unique=True,
        ),
        sa.Column("cap_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "soft_pct", sa.Numeric(5, 2), nullable=False, server_default="80"
        ),
        sa.Column(
            "hard_pct", sa.Numeric(5, 2), nullable=False, server_default="100"
        ),
        sa.Column(
            "spent_usd", sa.Numeric(12, 6), nullable=False, server_default="0"
        ),
    )

    # ---- action_ledger_events ----
    op.create_table(
        "action_ledger_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=True, index=True
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("action_kind", sa.String(64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_kind", sa.String(64), nullable=True),
        sa.Column(
            "payload_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("prev_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chain_hash", sa.String(64), nullable=False),
    )
    op.create_index(
        "ix_ledger_project_time",
        "action_ledger_events",
        ["project_id", "timestamp"],
    )
    op.create_index(
        "ix_ledger_action_kind", "action_ledger_events", ["action_kind"]
    )
    op.create_index(
        "ix_action_ledger_events_timestamp",
        "action_ledger_events",
        ["timestamp"],
    )

    # Immutability triggers — defense-in-depth on top of service-layer rule.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_immutable() RETURNS TRIGGER AS $$
        BEGIN
          RAISE EXCEPTION 'action_ledger_events is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_no_update BEFORE UPDATE ON action_ledger_events
          FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_no_delete BEFORE DELETE ON action_ledger_events
          FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
        """
    )

    # ---- ledger_chain_heads ----
    op.create_table(
        "ledger_chain_heads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            unique=True,
        ),
        sa.Column("head_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "head_chain_hash",
            sa.String(64),
            nullable=False,
            server_default="0" * 64,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Partial unique index for the null-project chain head (NULL != NULL in
    # plain unique constraints, so we need an explicit partial).
    op.execute(
        """
        CREATE UNIQUE INDEX uq_chain_head_global
          ON ledger_chain_heads ((1))
          WHERE project_id IS NULL;
        """
    )

    # ---- agent_runs ----
    op.create_table(
        "agent_runs",
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
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True
        ),
        sa.Column("agent_kind", sa.String(64), nullable=False, index=True),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "input_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("result_payload", postgresql.JSONB(), nullable=True),
        sa.Column("error_text", sa.String(4096), nullable=True),
        sa.UniqueConstraint(
            "correlation_id", name="uq_agent_runs_correlation_id"
        ),
    )

    # ---- agent_events ----
    op.create_table(
        "agent_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
        ),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column(
            "payload_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # ---- model_calls ----
    op.create_table(
        "model_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True
        ),
        sa.Column(
            "agent_run_id", postgresql.UUID(as_uuid=True), nullable=True, index=True
        ),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(128), nullable=False, index=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"
        ),
        sa.Column(
            "cache_savings_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # ---- cost_ledger_events ----
    op.create_table(
        "cost_ledger_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True
        ),
        sa.Column(
            "agent_run_id", postgresql.UUID(as_uuid=True), nullable=True, index=True
        ),
        sa.Column(
            "model_call_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
        ),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # Budget rollup trigger — sums into budgets.spent_usd on every cost event.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION budget_rollup() RETURNS TRIGGER AS $$
        BEGIN
          UPDATE budgets
          SET spent_usd = spent_usd + NEW.cost_usd
          WHERE project_id = NEW.project_id;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER cost_to_budget AFTER INSERT ON cost_ledger_events
          FOR EACH ROW EXECUTE FUNCTION budget_rollup();
        """
    )

    # ---- seed model profile templates ----
    system_user_id = str(uuid4())
    dev_id = uuid4()
    prod_id = uuid4()

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO model_profiles (
                id, name, project_id, is_template, bindings_jsonb,
                created_by, access_scope
            ) VALUES
              (:dev_id, 'aleph-dev', NULL, true, :dev_bindings,
               :sys, 'global'),
              (:prod_id, 'aleph-production', NULL, true, :prod_bindings,
               :sys, 'global')
            """
        ),
        {
            "dev_id": str(dev_id),
            "prod_id": str(prod_id),
            "sys": system_user_id,
            "dev_bindings": json.dumps(_ALEPH_DEV_BINDINGS),
            "prod_bindings": json.dumps(_ALEPH_PROD_BINDINGS),
        },
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS cost_to_budget ON cost_ledger_events")
    op.execute("DROP FUNCTION IF EXISTS budget_rollup")
    op.execute("DROP TRIGGER IF EXISTS ledger_no_update ON action_ledger_events")
    op.execute("DROP TRIGGER IF EXISTS ledger_no_delete ON action_ledger_events")
    op.execute("DROP FUNCTION IF EXISTS ledger_immutable")

    op.drop_table("cost_ledger_events")
    op.drop_table("model_calls")
    op.drop_table("agent_events")
    op.drop_table("agent_runs")
    op.execute("DROP INDEX IF EXISTS uq_chain_head_global")
    op.drop_table("ledger_chain_heads")
    op.drop_index("ix_action_ledger_events_timestamp", "action_ledger_events")
    op.drop_index("ix_ledger_action_kind", "action_ledger_events")
    op.drop_index("ix_ledger_project_time", "action_ledger_events")
    op.drop_table("action_ledger_events")
    op.drop_table("budgets")
    op.drop_table("model_profiles")
    op.drop_table("project_members")
    op.drop_table("projects")
    op.drop_table("users")
