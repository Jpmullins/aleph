"""drop budgets

Removes the budget subsystem entirely. It was never load-bearing: the only
enforcement site in the whole repo was the OWNER-gated smoke-test route, and
nothing on any production LLM path ever consulted a cap. Cost *tracking*
(`model_calls` + `cost_ledger_events`, load-bearing rule #5) is untouched —
`GET /v1/projects/{id}/cost` now sums the model-call rows on read instead of
reading a trigger-maintained `budgets.spent_usd`.

Dropped:
  * trigger `cost_to_budget` on `cost_ledger_events` and function `budget_rollup()`
  * table `budgets`
  * column `projects.budget_id`

Revision ID: drop_budgets
Revises: wp6_trust_layer
Create Date: 2026-07-26

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "drop_budgets"
down_revision: str | None = "wp6_trust_layer"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The trigger references `budgets`, so it must go first.
    op.execute("DROP TRIGGER IF EXISTS cost_to_budget ON cost_ledger_events")
    op.execute("DROP FUNCTION IF EXISTS budget_rollup")
    op.drop_column("projects", "budget_id")
    op.drop_table("budgets")


def downgrade() -> None:
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
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cap_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("soft_pct", sa.Numeric(5, 2), nullable=False, server_default="80"),
        sa.Column("hard_pct", sa.Numeric(5, 2), nullable=False, server_default="100"),
        sa.Column("spent_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
    )
    op.create_index(op.f("ix_budgets_project_id"), "budgets", ["project_id"], unique=False)
    op.create_unique_constraint("uq_budgets_project_id", "budgets", ["project_id"])
    op.add_column(
        "projects",
        sa.Column("budget_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
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
