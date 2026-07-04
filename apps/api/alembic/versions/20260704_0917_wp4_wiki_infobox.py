"""wp4 wiki page infobox_jsonb

Revision ID: wp4_wiki_infobox
Revises: wp2_scholar_connectors
Create Date: 2026-07-04 09:17:00.000000+00:00

Adds a nullable `infobox_jsonb` to `wiki_pages` (WP-4 sub-spec b). It holds the
optional key/value pairs the curator may populate for the deterministic HTML
compiler's infobox table. Absent (NULL) = no infobox. Markdown remains the only
wiki write-format; this column is metadata read by the compiler, never a body.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "wp4_wiki_infobox"
down_revision: str | None = "wp2_scholar_connectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wiki_pages",
        sa.Column(
            "infobox_jsonb",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("wiki_pages", "infobox_jsonb")
