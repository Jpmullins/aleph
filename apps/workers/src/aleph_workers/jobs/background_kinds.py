"""What a ticket can actually ask for, and the handler that does it.

Two kinds ship in V1, and both are real work the analyst already wants and
cannot currently get without waiting: reindex everything that never got indexed,
and put every substantive page through mechanical review. Both are fan-outs over
a list, which is what makes cancellation meaningful — there is a boundary
between units at which stopping is clean.

**The registry is bound to names that live in ``aleph_db``**
(``BACKGROUND_TASK_KINDS``) rather than declared here, because the API validates
a requested kind and apps never import apps. ``test_every_kind_has_a_handler``
asserts the two agree in both directions: a kind the route accepts with no
handler is a ticket that can only fail, and a handler nobody can request is dead
code.

**Both handlers are capped.** An uncapped sweep over a six-hundred-page corpus
is one tool call that spends the whole budget, and this workstream's own risk
note names that: a general "the agent can dispatch background work" primitive
without a bound is a way for a confused agent to do a great deal at once. The
cap is a stated number here rather than a hope about corpus size, and the result
payload reports when it bit so a truncated sweep is never mistaken for a
complete one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from aleph_rks.backfill import unindexed_document_ids
from aleph_wiki.models import WikiPage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aleph_workers.jobs.background import BackgroundTask

#: Units of work per ticket. Reached → the sweep stops and says so.
MAX_UNITS_PER_TASK = 200


async def reindex_corpus(task: BackgroundTask) -> dict[str, Any]:
    """Index every normalized document in the project that has no chunks.

    The repair for the failure that killed retrieval for seven work packages: 75
    ingested sources and zero chunk rows. ``backfill_index_job`` already does
    this in one shot; running it per document through a ticket is what makes it
    interruptible and observable while it runs, instead of a single opaque call
    that either returns in an hour or does not.
    """
    async with task.step("plan") as p:
        async with task.maker() as session:
            doc_ids = await unindexed_document_ids(session, project_id=task.project_id)
        capped = doc_ids[:MAX_UNITS_PER_TASK]
        p["documents_found"] = len(doc_ids)
        p["documents_planned"] = len(capped)

    enqueued = 0
    for doc_id in capped:
        # Before the step, never inside it: a `phase_started` for work that will
        # not happen is indistinguishable in the timeline from work that failed
        # without saying so.
        if await task.cancelled():
            break
        async with task.step("index_document", normalized_document_id=str(doc_id)):
            await task.enqueue("chunk_embed_job", str(doc_id), task.agent_token())
            enqueued += 1

    return {
        "documents_found": len(doc_ids),
        "documents_enqueued": enqueued,
        "capped": len(doc_ids) > MAX_UNITS_PER_TASK,
    }


async def review_sweep(task: BackgroundTask) -> dict[str, Any]:
    """Put every substantive page's current revision through mechanical review.

    Stubs are excluded and so are pages with no current revision: a stub is a red
    link nobody wrote, and reviewing an empty page produces a finding about the
    absence of content, which is not a finding.
    """
    async with task.step("plan") as p:
        async with task.maker() as session:
            rows = (
                await session.execute(
                    select(WikiPage.id, WikiPage.current_revision_id)
                    .where(
                        WikiPage.project_id == task.project_id,
                        WikiPage.is_stub.is_(False),
                        WikiPage.current_revision_id.is_not(None),
                    )
                    .order_by(WikiPage.title)
                )
            ).all()
        pages = [(page_id, rev_id) for page_id, rev_id in rows if rev_id is not None]
        capped = pages[:MAX_UNITS_PER_TASK]
        p["pages_found"] = len(pages)
        p["pages_planned"] = len(capped)

    enqueued = 0
    for page_id, revision_id in capped:
        if await task.cancelled():
            break
        async with task.step("review_page", page_id=str(page_id)):
            await task.enqueue(
                "mechanical_review_job",
                str(task.project_id),
                str(revision_id),
                str(page_id),
                task.agent_token(),
            )
            enqueued += 1

    return {
        "pages_found": len(pages),
        "pages_enqueued": enqueued,
        "capped": len(pages) > MAX_UNITS_PER_TASK,
    }


#: kind → handler. Keys must equal ``aleph_db.repos.background_tasks
#: .BACKGROUND_TASK_KINDS``; the test asserts it in both directions.
BACKGROUND_TASK_HANDLERS: dict[str, Callable[[BackgroundTask], Awaitable[dict[str, Any]]]] = {
    "reindex_corpus": reindex_corpus,
    "review_sweep": review_sweep,
}


__all__ = ["BACKGROUND_TASK_HANDLERS", "MAX_UNITS_PER_TASK", "reindex_corpus", "review_sweep"]
