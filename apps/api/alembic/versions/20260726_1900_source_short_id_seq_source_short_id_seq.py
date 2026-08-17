"""Allocate `Source.short_id` from a sequence, not from `COUNT(*) + 1`.

`_next_short_id` counted the rows and added one. `short_id` carries a **global**
unique constraint, so that is a lost-update race with a 500 at the end of it:

    n = SELECT count(*) FROM sources     -- every concurrent caller reads 588
    return f"S{n + 1:04d}"               -- every one of them returns "S0589"

The first insert wins and the rest raise `UniqueViolationError`, surfacing to
the agent as `sources/ingest-url failed (500)`. It only bites under
concurrency — which is precisely what a research run is: eight papers ingested
in the same second, one succeeds, seven fail. Sequentially it looks perfect,
which is why it shipped.

Counting is also wrong in a second, quieter way: it is not monotonic. Delete or
hard-remove any source and the count drops, so the next allocation **reuses an
id that is already referenced** by `[[Source:S0042]]` markers in committed wiki
prose. That silently re-points existing citations at a different paper.

A sequence is atomic, gap-tolerant, and never goes backwards. It is started
above the highest id already in use so no existing citation is ever reissued.

Revision ID: source_short_id_seq
Revises: gateway_pricing
"""

from __future__ import annotations

from alembic import op

revision: str = "source_short_id_seq"
down_revision: str | None = "gateway_pricing"
branch_labels: str | None = None
depends_on: str | None = None

SEQUENCE = "sources_short_id_seq"


def upgrade() -> None:
    op.execute(f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE} AS bigint START WITH 1")
    # Start above every `S<digits>` id already allocated. Ids that do not match
    # that shape (test fixtures use random suffixes) cannot collide with a
    # zero-padded numeric one, so they are correctly ignored here.
    op.execute(
        f"""
        SELECT setval(
            '{SEQUENCE}',
            GREATEST(
                (SELECT COALESCE(MAX(CAST(SUBSTRING(short_id FROM 2) AS bigint)), 0)
                   FROM sources
                  WHERE short_id ~ '^S[0-9]+$'),
                0
            ) + 1,
            false
        )
        """
    )


def downgrade() -> None:
    op.execute(f"DROP SEQUENCE IF EXISTS {SEQUENCE}")
