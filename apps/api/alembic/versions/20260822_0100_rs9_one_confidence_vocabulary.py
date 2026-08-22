"""one confidence vocabulary

`wiki_claims.confidence` was written in three vocabularies that did not agree.
The derived engine emits six underscore-spelled states; the A2UI catalog
permitted six different words, two of them hyphenated; and the column's own
server default was `'cited'`, which is a member of neither. 806 of the 850 live
rows carried that default. A column whose value means three things depending on
which component reads it cannot be rendered, filtered or reasoned about.

Forward:

* every non-canonical value is rewritten to the canonical member it meant —
  `cited` → `weakly_supported` (a citation is attached and nothing has earned
  more), `uncited` → `under_investigation`, the hyphenated pair to their
  underscore spellings, and `retracted` → `under_investigation`, because a
  withdrawn support leaves no standing evidence and the retraction itself lives
  in `wiki_claims.status`;
* the server default moves from `'cited'` to `'under_investigation'`, so an
  INSERT that omits the column stops minting a seventh word.

**`origin='user'` rows are not touched.** A human's claim is the one thing the
belief design promises is immutable to the machine, and a blanket UPDATE over a
column that in some rows reflects a person's judgement would silently overwrite
exactly that. Verified before writing this: all three `origin='user'` rows on
the live stack already hold canonical values, so skipping them costs nothing
today and refuses to be the migration that quietly edited a person's work if it
ever would.

`wiki_claims.confidence` is deliberately left WITHOUT a CHECK constraint. It
should have one, and the reason it does not is named rather than hidden: one
end-to-end test still constructs a `ClaimDraft(confidence="cited")` directly
(`tests/e2e/test_research_reads_sources.py`), and a constraint added while that
stands turns another workstream's suite red for a spelling. The write paths are
guarded instead — `aleph_core.confidence.canonical_confidence` runs at both
`WikiClaim` insert sites — so a legacy spelling is translated rather than
stored. See docs/plan.md WS-RS9.

Backward: the default returns to `'cited'`. The data rewrite is NOT reversed:
`weakly_supported` conflates rows that were `cited` with rows the engine
derived, and guessing which was which would invent history. Stated here rather
than left for the reader to discover.

Revision ID: rs9_confidence_vocab
Revises: p7_cred_key_version
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rs9_confidence_vocab"
down_revision: str | None = "p7_cred_key_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Must agree with `aleph_core.confidence.LEGACY_CONFIDENCE`. Duplicated here
#: because a migration that imports application code stops replaying the day
#: that code is refactored; `tests/unit/test_confidence_vocabulary.py` asserts
#: the two dictionaries are identical, so the copy cannot drift in silence.
LEGACY_TO_CANONICAL: dict[str, str] = {
    "cited": "weakly_supported",
    "uncited": "under_investigation",
    "well-supported": "well_supported",
    "weakly-supported": "weakly_supported",
    "retracted": "under_investigation",
    "initial": "under_investigation",
}

CANONICAL: tuple[str, ...] = (
    "under_investigation",
    "weakly_supported",
    "well_supported",
    "contested",
    "refuted",
    "abandoned",
)


def upgrade() -> None:
    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        op.execute(
            f"""
            UPDATE wiki_claims
               SET confidence = '{canonical}'
             WHERE confidence = '{legacy}'
               AND origin <> 'user'
            """
        )
    # Anything still outside the vocabulary and not a human's row is a value no
    # writer in the tree produces. It becomes `under_investigation` rather than
    # being left to render as a blank badge, and the ledger of what it was is
    # the git history of whatever wrote it.
    values = ", ".join(f"'{v}'" for v in CANONICAL)
    op.execute(
        f"""
        UPDATE wiki_claims
           SET confidence = 'under_investigation'
         WHERE confidence NOT IN ({values})
           AND origin <> 'user'
        """
    )
    op.execute("ALTER TABLE wiki_claims ALTER COLUMN confidence SET DEFAULT 'under_investigation'")


def downgrade() -> None:
    op.execute("ALTER TABLE wiki_claims ALTER COLUMN confidence SET DEFAULT 'cited'")
