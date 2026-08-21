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
    """The queue is `status='draft'`; a stub must never be in it."""
    stray = (
        await session.execute(
            text("SELECT count(*) FROM wiki_pages WHERE is_stub IS TRUE AND status = 'draft'")
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
