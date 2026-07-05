"""wp6 trust layer: freshness + source retraction columns

Revision ID: wp6_trust_layer
Revises: wp4_card_spotlight
Create Date: 2026-07-04 12:00:00.000000+00:00

WP-6 §1. Additive, nullable/defaulted columns only:

- ``wiki_pages``: ``volatility`` (String(8), default ``"warm"`` — hot|warm|cold),
  ``verified_at`` (timestamptz, nullable), ``freshness`` (SmallInteger, nullable — 0-100).
- ``sources``: ``retracted_at`` (timestamptz, nullable), ``retraction_reason``
  (Text, nullable). ``sources.status`` gains the free-string value ``"retracted"``
  and ``wiki_claims.confidence``/``status`` gain ``"retracted"``/``"contested"``
  with no schema change (both are free String columns).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "wp6_trust_layer"
down_revision: str | None = "wp4_card_spotlight"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wiki_pages",
        sa.Column(
            "volatility",
            sa.String(length=8),
            server_default=sa.text("'warm'"),
            nullable=False,
        ),
    )
    op.add_column(
        "wiki_pages",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wiki_pages",
        sa.Column("freshness", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "sources",
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sources",
        sa.Column("retraction_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sources", "retraction_reason")
    op.drop_column("sources", "retracted_at")
    op.drop_column("wiki_pages", "freshness")
    op.drop_column("wiki_pages", "verified_at")
    op.drop_column("wiki_pages", "volatility")
