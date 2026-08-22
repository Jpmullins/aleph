"""`commit_revision` under real concurrency — the two races it used to lose.

This file exists because a sequential test cannot fail on either defect. Both
are lost races, and a race needs two transactions genuinely in flight at the
same moment against the same Postgres; a mock session, a `for` loop, or eight
awaits on one session all serialize themselves and report green over a broken
write path. So every test here opens its own `AsyncSession` and runs the batch
through `asyncio.gather`.

The two defects, both on the agent path — four call sites pass a literal
`page_id=None`, and the wiki ingest workflow passes a `UUID | None` that is None
for any page it is minting, so concurrent ingest of two sources that name the
same topic hits this every time:

1. `_lock_or_create_page(page_id=None)` did a plain SELECT and returned the row
   UNLOCKED. `commit_revision` then computed `revision_no = max + 1` with
   nothing held, against UNIQUE(page_id, revision_no). Two commits both read
   max=N, both inserted N+1, and the loser raised IntegrityError out of the
   service as an unhandled 500 with its work discarded.
2. The same SELECT lost the create race: two commits minting the same NEW title
   both saw "does not exist" and collided on uq_wiki_pages_project_slug.

Verified to fail before the fix, which is the sentence that matters here. With
the two `retry_on_unique_violation` tests removed — they import symbols the
pre-fix module does not define, so the file cannot even be collected against it
— the pre-fix `wiki_service.py` reports 3 failed, 1 passed:

* `test_concurrent_commits_to_an_existing_page_number_sequentially` dies on
  `asyncpg.exceptions.UniqueViolationError: duplicate key value violates unique
  constraint "uq_wiki_rev_page_no"` — race 1, with race 2 taken out of the way.
* the two that mint a new title die on `... "uq_wiki_pages_project_slug"` —
  race 2, which fires first whenever the page does not exist yet.

All six pass against the fixed one.

NOTE on the warm-up commit every racing test does first. `LedgerWriter`'s
`_lock_or_create_head` has the identical create race on `ledger_chain_heads`
(project_id is UNIQUE, and the create is a SELECT-then-INSERT), so a project
whose very first ledger event is written by eight concurrent sessions collides
there instead — masking the defect under test with a different one. One
sequential commit first creates the head row; from then on the chain-head lock
serializes and the wiki races are what remains. That ledger race is real and
lives in `packages/aleph-db/src/aleph_db/repos/ledger.py`, which this workstream
does not own.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_core.errors import Conflict
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal
from aleph_wiki.models import WikiRevision
from aleph_wiki.wiki_service import (
    MAX_COMMIT_ATTEMPTS,
    CommitResult,
    WikiService,
    retry_on_unique_violation,
)

pytestmark = pytest.mark.integration

#: Eight, because the defect is probabilistic in the number of writers and two
#: concurrent sessions lose the race often but not always. Eight loses it on
#: every run observed.
RACERS = 8

#: The wiki tables `committed_project`'s teardown does not know about, in delete
#: order. Scoped to one project id for the same reason the shared fixture is: a
#: truncate-everything teardown against the compose Postgres — which is the
#: documented way to run these — destroys a real corpus.
#:
#: `wiki_revisions` is deliberately absent. It carries an append-only trigger
#: (`wiki_revisions_no_delete`), and `aleph` is a superuser in the compose stack,
#: so a teardown *could* bypass it with `session_replication_role`. It must not:
#: a fixture that switches off a core invariant to tidy up is how the invariant
#: stops being one. The same reasoning conftest applies to `action_ledger_events`.
#: The rows left behind belong to a throwaway project id and interfere with
#: nothing — including `test_no_page_has_two_revisions_with_the_same_number`,
#: which they give more to check rather than less.
_WIKI_TEARDOWN_SQL = (
    "DELETE FROM citations WHERE project_id = :pid",
    "DELETE FROM wiki_claims WHERE project_id = :pid",
    "DELETE FROM wiki_links WHERE project_id = :pid",
    "DELETE FROM wiki_sections WHERE project_id = :pid",
    "DELETE FROM wiki_index WHERE project_id = :pid",
    "DELETE FROM wiki_pages WHERE project_id = :pid",
)


@pytest.fixture
async def wiki_project(
    committed_project: uuid.UUID, maker: Callable[[], AsyncSession]
) -> AsyncIterator[uuid.UUID]:
    """`committed_project` plus a teardown for the wiki tables it does not cover."""
    yield committed_project
    async with maker() as s:
        for statement in _WIKI_TEARDOWN_SQL:
            await s.execute(text(statement), {"pid": committed_project})
        await s.commit()


@pytest.fixture
def principal() -> Principal:
    actor = uuid.uuid4()
    return Principal(
        user_id=actor,
        subject=str(actor),
        email="concurrency@test.invalid",
        actor_kind="user",
    )


async def _commit(
    maker: Callable[[], AsyncSession],
    *,
    project_id: uuid.UUID,
    principal: Principal,
    title: str,
    body_md: str,
    page_id: uuid.UUID | None = None,
) -> CommitResult:
    """One commit in its own session and its own transaction.

    A separate session per caller is the whole point: sharing one would make
    these commits sequential and the test would pass against the broken code.

    ``page_id`` selects the branch under test. `None` is the by-title path;
    passing an id is the by-ID path, which every HTTP commit
    (`POST /wiki/pages/{id}/commit`), the curator and the refresh job take —
    and which had no coverage at all, so an unlocked read there passed the whole
    gate.
    """
    async with maker() as session:
        result = await WikiService(session).commit_revision(
            principal=principal,
            ledger=LedgerWriter(session),
            project_id=project_id,
            page_id=page_id,
            title=title,
            slug=None,
            page_kind="topic",
            body_md=body_md,
            summary=body_md[:120],
            claims=[],
            wikilinks=[],
            commit_message="concurrency probe",
        )
        await session.commit()
        return result


async def _warm_up_ledger_head(
    maker: Callable[[], AsyncSession], *, project_id: uuid.UUID, principal: Principal
) -> None:
    """See the module docstring: create `ledger_chain_heads` sequentially so the
    ledger's own create race does not stand in for the one under test."""
    await _commit(
        maker,
        project_id=project_id,
        principal=principal,
        title="Ledger Warm Up",
        body_md="# Warm up\n\nSeeds the project's ledger chain head.\n",
    )


async def test_concurrent_by_title_commits_do_not_collide(
    maker: Callable[[], AsyncSession], wiki_project: uuid.UUID, principal: Principal
) -> None:
    """Eight concurrent by-title commits to one page all land, with 1..8.

    FAILS against the pre-fix service with UniqueViolationError on
    uq_wiki_rev_page_no: every session read the same `max(revision_no)` because
    nothing held a lock on the page row.

    Bodies must differ — `commit_revision` short-circuits on an unchanged
    body_sha256, and eight identical bodies would exercise the no-op path
    instead of the numbering path.
    """
    title = f"Race Target {uuid.uuid4().hex[:8]}"
    await _warm_up_ledger_head(maker, project_id=wiki_project, principal=principal)

    results = await asyncio.gather(
        *(
            _commit(
                maker,
                project_id=wiki_project,
                principal=principal,
                title=title,
                body_md=f"# {title}\n\nWriter {i} was here.\n",
            )
            for i in range(RACERS)
        )
    )

    page_ids = {r.page_id for r in results}
    assert len(page_ids) == 1, f"{RACERS} commits to one title produced {len(page_ids)} pages"
    page_id = page_ids.pop()

    async with maker() as s:
        numbers = sorted(
            (
                await s.execute(
                    text(
                        "SELECT revision_no FROM wiki_revisions WHERE page_id = :page_id "
                        "ORDER BY revision_no"
                    ),
                    {"page_id": page_id},
                )
            )
            .scalars()
            .all()
        )

    assert numbers == list(range(1, RACERS + 1)), (
        f"expected revisions 1..{RACERS}, got {numbers} — a gap means a commit was "
        "lost, a repeat means the unique constraint did not hold"
    )
    assert sorted(r.revision_no for r in results) == numbers, (
        "the numbers the service reported to its callers disagree with the rows in "
        "the table; a caller that trusts its CommitResult would be reading a "
        "revision it did not write"
    )


async def test_concurrent_commits_to_an_existing_page_number_sequentially(
    maker: Callable[[], AsyncSession], wiki_project: uuid.UUID, principal: Principal
) -> None:
    """The revision-numbering race on its own, with the create race taken away.

    The test above starts from nothing, so the create race fires first and the
    numbering race never gets a turn. Here the page already exists — which is
    the ordinary case: a topic page that two sources both add to — so every
    racer takes the existing-page branch and the only thing left to lose is
    `revision_no = max + 1`.

    FAILS against the pre-fix service with UniqueViolationError on
    uq_wiki_rev_page_no.
    """
    title = f"Existing Page {uuid.uuid4().hex[:8]}"
    await _commit(
        maker,
        project_id=wiki_project,
        principal=principal,
        title=title,
        body_md=f"# {title}\n\nFirst writer, sequential.\n",
    )

    results = await asyncio.gather(
        *(
            _commit(
                maker,
                project_id=wiki_project,
                principal=principal,
                title=title,
                body_md=f"# {title}\n\nConcurrent writer {i}.\n",
            )
            for i in range(RACERS)
        )
    )

    page_id = results[0].page_id
    async with maker() as s:
        numbers = sorted(
            (
                await s.execute(
                    text("SELECT revision_no FROM wiki_revisions WHERE page_id = :page_id"),
                    {"page_id": page_id},
                )
            )
            .scalars()
            .all()
        )
    assert numbers == list(range(1, RACERS + 2)), (
        f"expected the seed revision plus {RACERS} concurrent ones as 1..{RACERS + 1}, "
        f"got {numbers}"
    )


async def test_concurrent_by_id_commits_number_sequentially(
    maker: Callable[[], AsyncSession], wiki_project: uuid.UUID, principal: Principal
) -> None:
    """The by-ID branch, which nothing covered.

    An adversarial review replaced the by-ID branch's locked select with an
    unlocked `session.get(WikiPage, page_id)` and watched the ENTIRE suite stay
    green — because every other test here passes `page_id=None` and takes the
    by-title path. That branch is not a corner: it is what
    `POST /wiki/pages/{id}/commit`, `curator_service` and the refresh job all
    use, so the most-travelled route through this function was the one with no
    test on it.

    Same property as the by-title case — `revision_no = max + 1` is only safe
    under a row lock — measured on the branch that actually carries production
    traffic.
    """
    title = f"By Id Page {uuid.uuid4().hex[:8]}"
    seed = await _commit(
        maker,
        project_id=wiki_project,
        principal=principal,
        title=title,
        body_md=f"# {title}\n\nSeed revision.\n",
    )

    results = await asyncio.gather(
        *(
            _commit(
                maker,
                project_id=wiki_project,
                principal=principal,
                title=title,
                body_md=f"# {title}\n\nBy-id writer {i}.\n",
                page_id=seed.page_id,
            )
            for i in range(RACERS)
        )
    )

    assert {r.page_id for r in results} == {seed.page_id}
    async with maker() as s:
        numbers = sorted(
            (
                await s.execute(
                    text("SELECT revision_no FROM wiki_revisions WHERE page_id = :page_id"),
                    {"page_id": seed.page_id},
                )
            )
            .scalars()
            .all()
        )
    assert numbers == list(range(1, RACERS + 2)), (
        f"expected 1..{RACERS + 1} on the by-ID path, got {numbers}"
    )


async def test_concurrent_creates_of_a_new_title_produce_one_page(
    maker: Callable[[], AsyncSession], wiki_project: uuid.UUID, principal: Principal
) -> None:
    """Eight concurrent commits minting the SAME NEW title create one page.

    FAILS against the pre-fix service with UniqueViolationError on
    uq_wiki_pages_project_slug: every session's plain SELECT said "does not
    exist" and every session inserted.

    This is the unreported half of the defect, and it is the one ingest hits:
    two sources that mention the same topic mint the same title at the same
    moment, and neither of them names a page id.
    """
    await _warm_up_ledger_head(maker, project_id=wiki_project, principal=principal)

    title = f"Brand New Topic {uuid.uuid4().hex[:8]}"
    slug = title.lower().replace(" ", "-")

    await asyncio.gather(
        *(
            _commit(
                maker,
                project_id=wiki_project,
                principal=principal,
                title=title,
                body_md=f"# {title}\n\nMinted by writer {i}.\n",
            )
            for i in range(RACERS)
        )
    )

    async with maker() as s:
        pages = (
            await s.execute(
                text("SELECT count(*) FROM wiki_pages WHERE project_id = :pid AND slug = :slug"),
                {"pid": wiki_project, "slug": slug},
            )
        ).scalar_one()

    assert pages == 1, (
        f"{RACERS} concurrent creates of '{slug}' produced {pages} pages; "
        "create-or-fetch is not atomic"
    )


async def test_no_page_has_two_revisions_with_the_same_number(session: AsyncSession) -> None:
    """The invariant, checked against whatever is actually in this database.

    The constraint makes a duplicate impossible today, which is exactly why this
    is worth asserting: if anyone ever drops `uq_wiki_rev_page_no` to "fix" the
    500s instead of fixing the race, the 500 disappears and silent history
    corruption takes its place. This is the check that would notice.
    """
    dupes = (
        await session.execute(
            text(
                "SELECT page_id, revision_no, count(*) FROM wiki_revisions "
                "GROUP BY 1, 2 HAVING count(*) > 1"
            )
        )
    ).all()
    assert dupes == [], f"pages with duplicate revision numbers: {dupes}"


async def test_retry_on_unique_violation_recovers_a_loser(
    session: AsyncSession, wiki_project: uuid.UUID, principal: Principal
) -> None:
    """The belt-and-braces half: a first attempt that loses the race is re-run.

    Exercised directly because the row lock makes it unreachable through
    `commit_revision` — which is the point of belt and braces, and also the
    reason it would rot untested. The operation here inserts a revision number
    that is already taken on its first call and a free one on its second,
    which is precisely the shape of a lost `max + 1`.
    """
    page_id = uuid.uuid4()
    taken = WikiRevision(
        id=uuid.uuid4(),
        page_id=page_id,
        project_id=wiki_project,
        revision_no=1,
        body_md="already here",
        summary="",
        author_kind="user",
        author_id=principal.user_id,
        body_sha256="0" * 64,
        commit_message="",
        ledger_event_id=uuid.uuid4(),
    )
    session.add(taken)
    await session.flush()

    calls: list[int] = []

    async def _insert_next() -> int:
        calls.append(1)
        revision_no = 1 if len(calls) == 1 else 2
        session.add(
            WikiRevision(
                id=uuid.uuid4(),
                page_id=page_id,
                project_id=wiki_project,
                revision_no=revision_no,
                body_md=f"attempt {len(calls)}",
                summary="",
                author_kind="user",
                author_id=principal.user_id,
                body_sha256="1" * 64,
                commit_message="",
                ledger_event_id=uuid.uuid4(),
            )
        )
        await session.flush()
        return revision_no

    assert await retry_on_unique_violation(session, _insert_next) == 2
    assert len(calls) == 2, "the losing attempt was not retried"

    written = sorted(
        (
            await session.execute(
                text("SELECT revision_no FROM wiki_revisions WHERE page_id = :pid"),
                {"pid": page_id},
            )
        )
        .scalars()
        .all()
    )
    assert written == [1, 2], (
        f"expected the seeded row and one retried insert, got {written} — a rolled-back "
        "attempt left rows behind, so the savepoint is not covering the whole operation"
    )
    await session.rollback()


async def test_retry_is_bounded_not_a_loop(
    session: AsyncSession, wiki_project: uuid.UUID, principal: Principal
) -> None:
    """A commit that can never win gives up, and says so as a 409.

    The risk this pins is the one named in the workstream: under heavy fan-out a
    retry that loops until it succeeds turns a lock conflict into a request that
    never returns, which is harder to diagnose than the 500 it replaced. The
    operation here always violates, so a loop would hang this test forever.
    """
    page_id = uuid.uuid4()
    session.add(
        WikiRevision(
            id=uuid.uuid4(),
            page_id=page_id,
            project_id=wiki_project,
            revision_no=1,
            body_md="already here",
            summary="",
            author_kind="user",
            author_id=principal.user_id,
            body_sha256="0" * 64,
            commit_message="",
            ledger_event_id=uuid.uuid4(),
        )
    )
    await session.flush()

    calls: list[int] = []

    async def _always_collides() -> int:
        calls.append(1)
        session.add(
            WikiRevision(
                id=uuid.uuid4(),
                page_id=page_id,
                project_id=wiki_project,
                revision_no=1,
                body_md=f"attempt {len(calls)}",
                summary="",
                author_kind="user",
                author_id=principal.user_id,
                body_sha256="1" * 64,
                commit_message="",
                ledger_event_id=uuid.uuid4(),
            )
        )
        await session.flush()
        return 1

    with pytest.raises(Conflict):
        await asyncio.wait_for(retry_on_unique_violation(session, _always_collides), timeout=20)
    assert len(calls) == MAX_COMMIT_ATTEMPTS, (
        f"the operation ran {len(calls)} times against a budget of {MAX_COMMIT_ATTEMPTS}"
    )

    # The outer transaction survived: the savepoint rollback left it usable, so
    # a caller can report the 409 and unwind cleanly instead of dying on
    # "current transaction is aborted".
    assert (
        await session.execute(
            text("SELECT count(*) FROM wiki_revisions WHERE page_id = :pid"),
            {"pid": page_id},
        )
    ).scalar_one() == 1
    await session.rollback()
