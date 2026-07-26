"""Make a model call explain its own cost.

`model_calls` recorded `cost_usd` but not enough to check it. Two consequences,
both live:

* **Cache writes were invisible.** They bill at a premium and are a subset of
  `input_tokens`, so a cached call's cost did not follow from the columns and
  looked like an arithmetic error rather than a recorded fact.
* **$0 was ambiguous.** An unknown model priced at zero was indistinguishable
  from a genuinely free call. The shipped pricing table matched *none* of the
  real gateway's model names, so this was not hypothetical: the ledger would
  have read $0.00 across the board while real money was spent.

`pricing_source` makes the second case self-declaring, and the rate columns
keep a historical cost re-derivable after the gateway changes its prices.

Revision ID: gateway_pricing
Revises: drop_budgets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "gateway_pricing"
down_revision: str | None = "drop_budgets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "model_calls",
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "model_calls",
        sa.Column("pricing_source", sa.String(length=16), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "model_calls",
        sa.Column("input_rate_usd", sa.Numeric(20, 12), nullable=False, server_default="0"),
    )
    op.add_column(
        "model_calls",
        sa.Column("output_rate_usd", sa.Numeric(20, 12), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("model_calls", "output_rate_usd")
    op.drop_column("model_calls", "input_rate_usd")
    op.drop_column("model_calls", "pricing_source")
    op.drop_column("model_calls", "cache_write_tokens")
