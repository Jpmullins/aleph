"""Retrieval finds a page by the words its body is written in, and walks only
the links its current revision still has.

Three defects are pinned here. All three were silent — nothing raised, nothing
logged, every step reported success — and all three lived in Postgres rather
than in a model call:

1. **Body-blind index.** `wiki_index.index_tsv` was built from title + summary
   + aliases and never from `body_md`, so a page was unreachable by anything it
   actually said. The trigger now folds the body in at `ts_rank` weight C,
   below the title (A) and summary/aliases (B) — see the `wiki_index_body`
   migration — and `IndexService.refresh_page` populates `body_text` from the
   current revision.
2. **Conjunctive query gate.** The gate was `plainto_tsquery`, which ANDs every
   term, so a question had to contain *every* indexed word of a page to match
   it at all. It is now `aleph_core.tsquery.or_tsquery`, which rewrites the
   already-parsed conjunction into a disjunction.
3. **Stale-link expansion.** `WikiFirstRetrievalRouter._expand` selected from
   `wiki_links` with no `src_revision_id` filter. Link rows are per-revision and
   are never deleted, so a link dropped three rewrites ago kept pulling its
   target into answer context forever.

Nothing here calls a model. Candidate generation and link expansion are pure
SQL, which is exactly where all three defects lived; routing through the
composer would have measured the LLM instead of the query.

What this file does NOT cover, so nobody reads more into a green run than is
there: the weighting *order* (that an exact title match outranks a page that
merely mentions that title in its body — the A/B/C split's stated reason), the
corpus/RAG surface (`tests/e2e/test_search_corpus.py`), and recall over the
labelled pair set (`python -m aleph_evals.retrieval_eval`). These assert
reachability, not ranking quality.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter
from aleph_core.ids import uuid7
from aleph_models.client import LiteLLMClient
from aleph_wiki.index_service import IndexService
from aleph_wiki.models import WikiLink, WikiPage, WikiRevision

pytestmark = pytest.mark.integration


_DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"

#: Teardown, in delete order, every statement scoped to one throwaway project
#: id. Named explicitly rather than reflected: these tests are documented to run
#: against the compose Postgres, which also holds a real corpus, and a
#: truncate-everything teardown there is a data-loss bug waiting for its first
#: victim.
#:
#: `wiki_revisions` is deliberately absent. It carries an append-only trigger
#: (`wiki_revisions_no_delete`), and `aleph` is a superuser in compose, so a
#: teardown *could* disable it with `session_replication_role`. It must not: a
#: fixture that switches off an invariant to tidy up is how the invariant stops
#: being one. The rows belong to a random project id and interfere with nothing.
_TEARDOWN_SQL = (
    "DELETE FROM wiki_index WHERE project_id = :pid",
    "DELETE FROM wiki_links WHERE project_id = :pid",
    "DELETE FROM wiki_pages WHERE project_id = :pid",
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Per-test rather than session-scoped: a session-scoped async engine binds its
    asyncpg pool to the first test's event loop, and every later test then fails
    with "attached to a different loop".
    """
    eng = create_async_engine(os.environ.get("DATABASE_URL", _DEFAULT_URL), poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory. `_expand` opens its own session, so it needs one."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def project_id(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[UUID]:
    """A throwaway project id whose wiki rows are really committed, then deleted.

    Committed rather than rolled back because `_expand` opens its own session
    and cannot see another transaction's uncommitted writes.
    """
    pid = uuid7()
    yield pid
    async with maker() as session:
        for statement in _TEARDOWN_SQL:
            await session.execute(text(statement), {"pid": pid})
        await session.commit()


# --- the fixture page -------------------------------------------------------
#
# The body shares NO content word with the title or the summary. That is the
# whole design: if the probe phrase also appeared in the title, a body-blind
# index would still return the page and these tests would prove nothing.

TITLE = "Coastal Sediment Study"
SLUG = "coastal-sediment-study"
SUMMARY = "An overview of the coastal sediment study and its scope."
BODY = """# Coastal Sediment Study

## Methodology

Cores were retrieved using a vibracorer deployed from a shallow-draft vessel.
Grain size distribution was determined by laser diffraction granulometry, and
the resulting spectra were deconvolved with an endmember mixing model.

## Findings

The dominant lithofacies is a moderately sorted quartzarenite exhibiting
hummocky cross-stratification, consistent with storm-dominated deposition
above fair-weather wave base.
"""

#: In BODY, in neither TITLE nor SUMMARY.
BODY_ONLY_TERM = "quartzarenite"
#: In TITLE. The control: proves the fixture and the index row exist at all.
TITLE_TERM = "coastal sediment"
#: In nothing. The guard against a `select_pages` that ignores its query and
#: hands back whatever it has — which is how the retrieval audit stayed green
#: while retrieval was returning the most-recently-indexed pages.
ABSENT_TERM = "xyzzyplugh"
#: A question phrased the way a person asks one. Every content word it stems to
#: — grain, size, distribut, deposit — is in the BODY and none is in the title
#: or summary, and it also carries words ("tell", "environ") the page does not
#: have at all, so it matches only under an index that covers bodies AND a gate
#: that ORs terms.
NATURAL_QUESTION = (
    "what does the grain size distribution tell us about the depositional environment"
)


async def _seed_page(
    maker: async_sessionmaker[AsyncSession],
    project_id: UUID,
    *,
    title: str = TITLE,
    slug: str = SLUG,
    body: str = BODY,
) -> tuple[UUID, UUID]:
    """Commit one wiki page at revision 1 and index it, the way a compile does.

    Returns (page_id, revision_id). Committed, not just flushed: `_expand` opens
    its own session and would not see an uncommitted row.
    """
    page_id, revision_id = uuid7(), uuid7()
    async with maker() as session:
        session.add(
            WikiPage(
                id=page_id,
                project_id=project_id,
                title=title,
                slug=slug,
                page_kind="topic",
                current_revision_id=revision_id,
                is_stub=False,
                status="approved",
                created_by=uuid4(),
            )
        )
        session.add(
            WikiRevision(
                id=revision_id,
                page_id=page_id,
                project_id=project_id,
                revision_no=1,
                body_md=body,
                summary=SUMMARY,
                author_kind="agent",
                author_id=uuid4(),
                parent_revision_id=None,
                body_sha256="0" * 64,
                commit_message="probe fixture",
                ledger_event_id=uuid7(),
            )
        )
        # Flush before indexing: refresh_page reads the page row and its current
        # revision back out of the session to build the index row.
        await session.flush()
        await IndexService(session).refresh_page(
            project_id=project_id, page_id=page_id, summary=SUMMARY
        )
        await session.commit()
    return page_id, revision_id


async def test_body_phrase_retrieves_its_page(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """A word that appears only in a page's body retrieves that page."""
    page_id, _ = await _seed_page(maker, project_id)

    async with maker() as session:
        index = IndexService(session)
        control = await index.select_pages(project_id=project_id, query=TITLE_TERM, top_k=8)
        body_hits = await index.select_pages(project_id=project_id, query=BODY_ONLY_TERM, top_k=8)
        absent_hits = await index.select_pages(project_id=project_id, query=ABSENT_TERM, top_k=8)

    # Checked first so a broken fixture reports as a broken fixture rather than
    # as the defect this test exists to catch.
    assert page_id in {h.page_id for h in control}, (
        "the control failed: title words did not retrieve the page, so this test "
        "is not measuring what it claims to measure"
    )
    assert page_id in {h.page_id for h in body_hits}, (
        f"{BODY_ONLY_TERM!r} appears in this page's body and nowhere else, and did "
        f"not retrieve it. wiki_index.index_tsv is not covering body_text — every "
        f"page is reachable only by its title, summary and aliases. "
        f"got {len(body_hits)} hit(s)."
    )
    # Without this, a select_pages that dropped its WHERE clause entirely would
    # pass both assertions above.
    assert absent_hits == [], (
        f"a query for {ABSENT_TERM!r}, which occurs nowhere in the fixture, "
        f"returned {len(absent_hits)} page(s) — retrieval is answering with rows "
        f"it did not match"
    )


async def test_natural_language_question_retrieves_its_page(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """A question that shares no word with the title still retrieves the page."""
    page_id, _ = await _seed_page(maker, project_id)

    async with maker() as session:
        hits = await IndexService(session).select_pages(
            project_id=project_id, query=NATURAL_QUESTION, top_k=8
        )

    assert page_id in {h.page_id for h in hits}, (
        "a natural-language question about the page's own subject retrieved "
        "nothing. Under plainto_tsquery / websearch_to_tsquery every term is "
        "ANDed, so a question must use every one of a page's indexed words to be "
        "a candidate at all; or_tsquery is what makes a partial match surface and "
        "simply rank lower."
    )


async def test_expansion_ignores_links_from_superseded_revisions(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """A link only a superseded revision has is not expanded into context."""
    src_id, first_rev = await _seed_page(maker, project_id)

    dropped_id, kept_id = uuid7(), uuid7()
    second_rev = uuid7()

    async with maker() as session:
        for pid, title, slug in (
            (dropped_id, "Dropped Target", "dropped-target"),
            (kept_id, "Kept Target", "kept-target"),
        ):
            session.add(
                WikiPage(
                    id=pid,
                    project_id=project_id,
                    title=title,
                    slug=slug,
                    page_kind="topic",
                    current_revision_id=None,
                    is_stub=False,
                    status="approved",
                    created_by=uuid4(),
                )
            )
        # Revision 2 supersedes revision 1 on the source page.
        session.add(
            WikiRevision(
                id=second_rev,
                page_id=src_id,
                project_id=project_id,
                revision_no=2,
                body_md=BODY,
                summary=SUMMARY,
                author_kind="agent",
                author_id=uuid4(),
                parent_revision_id=first_rev,
                body_sha256="1" * 64,
                commit_message="dropped a link",
                ledger_event_id=uuid7(),
            )
        )
        # The superseded revision linked to Dropped; the current one links to
        # Kept. Both rows stay in wiki_links forever — the write path deletes
        # links for the revision it is about to create, which is always empty.
        session.add(
            WikiLink(
                id=uuid7(),
                project_id=project_id,
                src_page_id=src_id,
                src_revision_id=first_rev,
                dst_page_id=dropped_id,
                dst_title="Dropped Target",
                # Deliberately the higher count: expansion orders by
                # occurrences, so an unfiltered query ranks the stale link FIRST
                # and it survives any truncation to a smaller limit.
                occurrences=9,
            )
        )
        session.add(
            WikiLink(
                id=uuid7(),
                project_id=project_id,
                src_page_id=src_id,
                src_revision_id=second_rev,
                dst_page_id=kept_id,
                dst_title="Kept Target",
                occurrences=1,
            )
        )
        await session.execute(
            update(WikiPage).where(WikiPage.id == src_id).values(current_revision_id=second_rev)
        )
        await session.commit()

    # `litellm` is never touched by _expand — the step is one SELECT and a
    # hydrate — and passing None is how this file keeps its promise that no
    # model is called. If _expand ever grows a model call, this goes red with an
    # AttributeError, which is the correct alarm.
    router = WikiFirstRetrievalRouter(session_maker=maker, litellm=cast("LiteLLMClient", None))
    expanded = await router._expand(
        project_id=project_id,
        primary_ids=[src_id],
        already_selected={src_id},
        limit=8,
    )
    got = {p.page_id for p in expanded}

    assert kept_id in got, (
        "the control failed: the CURRENT revision's link was not followed, so a "
        "green 'stale link ignored' below would only mean expansion returned "
        "nothing at all"
    )
    assert dropped_id not in got, (
        "expansion followed a link that exists only on a superseded revision. "
        "wiki_links rows are per-revision and never deleted, so without the "
        "src_revision_id = current_revision_id join a page rewritten N times "
        "walks the union of every link it has ever had, and a link removed three "
        "revisions ago keeps pulling its target into answer context forever."
    )
