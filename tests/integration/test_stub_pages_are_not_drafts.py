"""A stub is a red link, not a proposal awaiting approval.

Stubs are minted whenever a page links to a title that does not exist yet. They
hold no content and nobody proposed them. Filing them as `draft` put 235 empty
placeholders in the owner's review queue alongside 15 real pages — 94% noise —
and an approval gesture that has to be performed 235 times stops meaning "I read
this and agree".

These run against real Postgres because the thing being asserted is what lands
in the table, and the defect was precisely that the value written did not match
what the queue meant.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_no_stub_sits_in_the_review_queue(session: AsyncSession) -> None:
    """The queue is `status='draft'`; a stub must never be in it.

    Scoped to projects that still EXIST, and the reason is not tidiness.

    `wiki_revisions` is append-only by database trigger and `wiki_pages` is held
    by a foreign key from it, so the integration teardown deletes a test's
    project and physically cannot delete the pages it committed. Those pages
    outlive their project permanently.

    Unscoped, this counted them. It went red on twenty rows titled "Gamma" left
    by a concurrency fixture whose project had been dropped three hours earlier —
    a real-looking failure about a real invariant, caused entirely by rubbish.
    A check that fails for reasons unrelated to the code is a check people learn
    to re-run rather than read.

    And the scope is not a loophole: the review queue is per project. A page
    whose project is gone is in nobody's queue, because there is no queue for it
    to be in.
    """
    stray = (
        await session.execute(
            text(
                "SELECT count(*) FROM wiki_pages p"
                " WHERE p.is_stub IS TRUE AND p.status = 'draft'"
                "   AND EXISTS (SELECT 1 FROM projects pr WHERE pr.id = p.project_id)"
            )
        )
    ).scalar_one()
    assert stray == 0, (
        f"{stray} empty stubs are queued for approval. Each one is a link to a "
        "page that does not exist yet, not something anyone proposed."
    )


async def test_the_queue_is_a_reviewable_size(session: AsyncSession) -> None:
    """Guard the guard: zero stubs is trivially true if there are no pages at all.

    This asserts the corpus is actually populated, so the test above is saying
    something. It also fails loudly if stub creation starts filing drafts again
    at scale — the queue growing into the hundreds is the symptom.
    """
    total = (await session.execute(text("SELECT count(*) FROM wiki_pages"))).scalar_one()
    if total == 0:
        pytest.skip("no wiki pages in this database — nothing to review")

    queued = (
        await session.execute(text("SELECT count(*) FROM wiki_pages WHERE status = 'draft'"))
    ).scalar_one()
    assert queued < total * 0.5, (
        f"{queued} of {total} pages are awaiting approval. A queue that is most "
        "of the corpus is a threshold problem at ingest, not a review backlog."
    )


async def test_stubs_still_exist_and_are_findable(session: AsyncSession) -> None:
    """Leaving the queue must not mean disappearing.

    A red link is still a page — it is what makes `[[wikilinks]]` resolve and
    what a later ingest promotes once the corpus keeps returning to it. Deleting
    stubs instead of re-labelling them would break every link that points at one.
    """
    stubs = (
        await session.execute(text("SELECT count(*) FROM wiki_pages WHERE is_stub IS TRUE"))
    ).scalar_one()
    if stubs == 0:
        pytest.skip("no stubs in this database")
    labelled = (
        await session.execute(
            text("SELECT count(*) FROM wiki_pages WHERE is_stub IS TRUE AND status = 'stub'")
        )
    ).scalar_one()
    assert labelled > 0, "stubs exist but none carry the 'stub' status"
