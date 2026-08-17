"""merge feat and refactor heads

Two branches forked from `wp6_trust_layer` and each grew a chain, so the
versions directory arrived at a merge with **two heads** and
`alembic upgrade head` became ambiguous:

    wp6_trust_layer ─┬─ drop_budgets ─ gateway_pricing ─ source_short_id_seq
                     └─ wiki_index_body ─ claim_spine

Git merged both chains cleanly because they are separate files; nothing in the
text conflicted. The DAG is where the collision lives.

This revision is empty on purpose. The two chains touch **disjoint** schema
objects, so no reconciling DDL is owed:

    feat      trigger `cost_to_budget`, function `budget_rollup()`,
              table `budgets`, column `projects.budget_id`,
              columns on `model_calls`, sequence `sources_short_id_seq`
    refactor  column `wiki_index.body_text`, function `wiki_index_tsv_update()`,
              columns/indexes on `wiki_claims` and `citations`,
              table `claim_edges`

The two sets share no table, column, index, sequence, function or trigger, and
neither chain reads an object the other drops — `citations.source_id` is a bare
UUID column with no foreign key, so it does not depend on anything feat touches,
and `source_short_id_seq` only reads `sources`, which refactor leaves alone.
Either application order therefore lands on the same schema.

Revision ID: merge_heads
Revises: source_short_id_seq, claim_spine
Create Date: 2026-08-17

"""

from __future__ import annotations

revision: str = "merge_heads"
down_revision: tuple[str, str] = ("source_short_id_seq", "claim_spine")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No-op: the merged chains are schema-disjoint."""


def downgrade() -> None:
    """No-op: unmerging restores two independent heads, each already reversible."""
