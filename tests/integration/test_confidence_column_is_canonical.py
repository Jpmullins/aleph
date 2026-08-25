"""No live claim carries a confidence outside the vocabulary.

`scripts/check-confidence-vocabulary.sh` proves the READERS agree. It cannot see
the column, and the column is where the drift actually lived: 806 of 850 rows
held `"cited"`, a word in none of the three vocabularies, because it was the
`wiki_claims.confidence` server default. A static sweep would have been green
for the entire life of that defect.

So this is the other half, and it runs against real Postgres deliberately — it
is a canary on the deployed instance, not a property of a fixture. It fails when
a writer reintroduces a spelling the migration removed, which is the recurrence
the write-path guard (`aleph_core.confidence.canonical_confidence`, called at
both `WikiClaim` insert sites) exists to prevent.

`hypotheses.confidence` used to be checked here too. That table is gone
(`docs/decisions.md` D16); the state machine that wrote it survives as
`aleph_belief.confidence.next_confidence_from_evidence` and now has exactly one
column to write — `wiki_claims.confidence`, checked above.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_core.confidence import CONFIDENCE_VALUES

pytestmark = pytest.mark.integration


async def test_no_wiki_claim_carries_a_word_outside_the_vocabulary(session: AsyncSession) -> None:
    rows = (await session.execute(text("SELECT DISTINCT confidence FROM wiki_claims"))).all()
    found = {row[0] for row in rows}
    stray = found - set(CONFIDENCE_VALUES)
    assert not stray, (
        f"wiki_claims.confidence holds {sorted(stray)}, which no reader recognises. "
        f"Canonical: {list(CONFIDENCE_VALUES)}. A writer is bypassing "
        f"aleph_core.confidence.canonical_confidence, or a migration was skipped."
    )


async def test_the_column_default_is_a_member(session: AsyncSession) -> None:
    """The default was the single biggest producer of the old drift.

    An INSERT that omits the column must not mint a word the readers do not
    have; that is exactly how one value reached 95% of the table without any
    code ever choosing it.
    """
    default = (
        await session.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'wiki_claims' AND column_name = 'confidence'"
            )
        )
    ).scalar_one()
    assert default is not None, "the column has no default; an omitted INSERT would fail"
    # Postgres reports it as `'value'::character varying`.
    literal = str(default).split("::")[0].strip("'")
    assert literal in CONFIDENCE_VALUES, (
        f"wiki_claims.confidence defaults to {literal!r}, which is not a member of "
        f"{list(CONFIDENCE_VALUES)}"
    )
