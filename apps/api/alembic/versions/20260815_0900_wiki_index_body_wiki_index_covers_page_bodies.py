"""wiki index covers page bodies

The retrieval index was built from title + summary + aliases and never the page
body, so a question phrased in the words a page is actually written in could not
retrieve that page. Combined with `plainto_tsquery` (which ANDs every term),
most natural-language questions matched nothing at all — the wiki was never
searchable, which is why the "wiki as primary retrieval surface" bet was never
actually tested. See docs/decisions.md D1.

Adds `wiki_index.body_text`, populated by `IndexService.refresh_page` from the
current revision, and rewrites the trigger to include it with a lower `ts_rank`
weight than the title:

    A  title            an exact title match should still win
    B  summary, aliases
    C  body

Weighting matters. Without it a long body's term frequency would swamp a title
match, and searching for a page by its name would start returning whichever page
mentions that name most often.

Existing rows are backfilled from the current revision in the same migration, so
the index is correct immediately rather than after the next compile of each page.

Revision ID: wiki_index_body
Revises: wp6_trust_layer
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "wiki_index_body"
down_revision = "wp6_trust_layer"
branch_labels = None
depends_on = None

_TRIGGER_WITH_BODY = """
CREATE OR REPLACE FUNCTION wiki_index_tsv_update() RETURNS TRIGGER AS $$
BEGIN
  NEW.index_tsv :=
      setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
      setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'B') ||
      setweight(to_tsvector('english', coalesce((
        SELECT string_agg(elem, ' ')
        FROM jsonb_array_elements_text(NEW.aliases_jsonb) AS t(elem)
      ), '')), 'B') ||
      setweight(to_tsvector('english', coalesce(NEW.body_text, '')), 'C');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_WITHOUT_BODY = """
CREATE OR REPLACE FUNCTION wiki_index_tsv_update() RETURNS TRIGGER AS $$
BEGIN
  NEW.index_tsv := to_tsvector('english',
      coalesce(NEW.title, '') || ' ' ||
      coalesce(NEW.summary, '') || ' ' ||
      coalesce((
        SELECT string_agg(elem, ' ')
        FROM jsonb_array_elements_text(NEW.aliases_jsonb) AS t(elem)
      ), '')
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column("wiki_index", sa.Column("body_text", sa.Text(), nullable=True))
    op.execute(_TRIGGER_WITH_BODY)

    # Backfill from each page's current revision, then re-fire the trigger with
    # a no-op UPDATE so index_tsv is rebuilt for rows that already existed.
    op.execute(
        """
        UPDATE wiki_index AS wi
        SET body_text = wr.body_md
        FROM wiki_pages AS wp
        JOIN wiki_revisions AS wr ON wr.id = wp.current_revision_id
        WHERE wp.id = wi.page_id
        """
    )
    op.execute("UPDATE wiki_index SET body_text = body_text")


def downgrade() -> None:
    op.execute(_TRIGGER_WITHOUT_BODY)
    op.execute("UPDATE wiki_index SET title = title")
    op.drop_column("wiki_index", "body_text")
