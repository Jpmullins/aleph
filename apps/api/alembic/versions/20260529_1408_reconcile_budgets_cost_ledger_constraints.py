"""reconcile_budgets_cost_ledger_constraint_naming

Reconcile constraint/index naming on `budgets` and `cost_ledger_events` to
match the ORM models. Inc 0 created these via column-level ``unique=True``,
which yielded a UNIQUE ``ix_budgets_project_id`` index and the auto-named
``cost_ledger_events_model_call_id_key`` constraint. The models now declare
named ``UniqueConstraint``s (``uq_budgets_project_id``,
``uq_cost_ledger_model_call``) alongside a plain (non-unique)
``ix_budgets_project_id`` index. This migration brings the live schema in line.

Revision ID: reconcile_budgets_cost_ledger
Revises: inc8_evals_feedback
Create Date: 2026-05-29 14:08:11.210545+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "reconcile_budgets_cost_ledger"
down_revision: str | None = "inc8_evals_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # budgets: replace the UNIQUE index on project_id with a plain index plus a
    # named unique constraint (matches Budget.__table_args__).
    op.drop_index(op.f("ix_budgets_project_id"), table_name="budgets")
    op.create_index(op.f("ix_budgets_project_id"), "budgets", ["project_id"], unique=False)
    op.create_unique_constraint("uq_budgets_project_id", "budgets", ["project_id"])

    # cost_ledger_events: rename the auto-named unique constraint to the
    # model-declared name (matches CostLedgerEvent.__table_args__).
    op.drop_constraint(
        op.f("cost_ledger_events_model_call_id_key"),
        "cost_ledger_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_cost_ledger_model_call", "cost_ledger_events", ["model_call_id"]
    )


def downgrade() -> None:
    # cost_ledger_events: restore the auto-named unique constraint.
    op.drop_constraint("uq_cost_ledger_model_call", "cost_ledger_events", type_="unique")
    op.create_unique_constraint(
        op.f("cost_ledger_events_model_call_id_key"),
        "cost_ledger_events",
        ["model_call_id"],
    )

    # budgets: restore the UNIQUE index on project_id.
    op.drop_constraint("uq_budgets_project_id", "budgets", type_="unique")
    op.drop_index(op.f("ix_budgets_project_id"), table_name="budgets")
    op.create_index(op.f("ix_budgets_project_id"), "budgets", ["project_id"], unique=True)
