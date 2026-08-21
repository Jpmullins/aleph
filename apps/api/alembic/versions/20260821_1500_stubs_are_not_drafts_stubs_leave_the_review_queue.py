"""stubs leave the review queue

A stub is minted whenever a page links to a title that does not exist yet. It
holds no content and nobody proposed it, so filing it as `draft` put 235 empty
placeholders in front of the owner for approval alongside 15 real pages — 94%
noise. An approval that must be performed 235 times cannot mean "I read this and
agree"; it can only mean "make the banner go away".

Forward: every existing stub still sitting in the queue moves to `status="stub"`.
Backward: they return to `draft`, restoring the old queue exactly.

Revision ID: stubs_not_drafts
Revises: merge_heads
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "stubs_not_drafts"
down_revision: str | None = "merge_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE wiki_pages
           SET status = 'stub'
         WHERE is_stub IS TRUE
           AND status = 'draft'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE wiki_pages
           SET status = 'draft'
         WHERE is_stub IS TRUE
           AND status = 'stub'
        """
    )
