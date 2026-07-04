"""wp4 interactive_cards spotlighted bit

Revision ID: wp4_card_spotlight
Revises: wp4_wiki_infobox
Create Date: 2026-07-04 11:30:00.000000+00:00

Adds a non-null `spotlighted` boolean (default false) to `interactive_cards`
(WP-4 sub-spec d). An agent `spotlight` card action flips this bit; the Briefs
surface builder orders spotlighted cards first and flags them in props. A
persisted column (rather than an ephemeral flag) survives a surface rebuild and
is queryable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "wp4_card_spotlight"
down_revision: str | None = "wp4_wiki_infobox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "interactive_cards",
        sa.Column(
            "spotlighted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("interactive_cards", "spotlighted")
