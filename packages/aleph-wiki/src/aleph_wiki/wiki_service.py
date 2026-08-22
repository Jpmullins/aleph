"""Wiki service: the only path that writes WikiPage / WikiRevision.

`commit_revision` is atomic: create-or-LOCK the page, apply HandEditMark
protection, compute body_sha256, no-op if unchanged, insert new revision,
replace sections, replace wikilinks, insert claims + citations, update the page
pointer, refresh WikiIndex, append ledger event.

"Atomic" used to be a claim this docstring made and the code did not keep. See
`_lock_or_create_page` and `retry_on_unique_violation` below for the two races
that lived here and what closes them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from aleph_core.errors import Conflict, NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id, start_span
from aleph_wiki import schema as schema_mod
from aleph_wiki.handedit_service import list_active_for_page
from aleph_wiki.index_service import IndexService
from aleph_wiki.models import (
    Citation,
    WikiClaim,
    WikiLink,
    WikiPage,
    WikiRevision,
    WikiSection,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


PageKind = Literal["topic", "source", "synthesis", "stub"]


@dataclass(frozen=True)
class ClaimDraft:
    text: str
    confidence: str = "cited"
    section_anchor: str | None = None
    citations: list[CitationDraft] = field(default_factory=list)


@dataclass(frozen=True)
class CitationDraft:
    chunk_ids: list[UUID]
    source_page_id: UUID | None
    citation_marker: str
    #: The retraction join key. `source_page_id` pointed at a SourcePage row and
    #: was the only anchor a citation had — and no production writer ever set
    #: it, so retraction blast-radius returned nothing for the life of the
    #: feature. This is the direct link, and the legacy compile path sets it too
    #: so that a claim written by either path is reachable from its source.
    source_id: UUID | None = None


@dataclass(frozen=True)
class WikiLinkDraft:
    dst_title: str
    dst_page_id: UUID | None
    occurrences: int = 1


@dataclass(frozen=True)
class SectionDraft:
    anchor: str
    char_start: int
    char_end: int
    ordinal: int


@dataclass(frozen=True)
class CommitResult:
    page_id: UUID
    revision_id: UUID
    revision_no: int
    body_sha256: str
    was_noop: bool


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)

#: The two unique constraints a concurrent wiki commit can lose a race to.
#: `uq_wiki_rev_page_no` = UNIQUE(page_id, revision_no); `uq_wiki_pages_project_slug`
#: = UNIQUE(project_id, slug). Named explicitly so the retry cannot swallow an
#: unrelated integrity failure — a NOT NULL violation or a bad foreign key is a
#: bug in the caller, and retrying it just runs the bug twice.
WIKI_RACE_CONSTRAINTS: tuple[str, ...] = ("uq_wiki_rev_page_no", "uq_wiki_pages_project_slug")

#: Two attempts, not "until it succeeds".
#:
#: With the page row locked FOR UPDATE the second attempt cannot lose the same
#: race, so a third could only be papering over a different bug. And an
#: unbounded retry under ingest fan-out converts a lock conflict into a livelock
#: that never surfaces anywhere — the request simply never returns, which is
#: harder to diagnose than the 500 this replaces.
MAX_COMMIT_ATTEMPTS = 2

#: Same reasoning for create-or-lock. One pass is sufficient in every case we
#: can construct (see `_lock_or_create_page`); the second exists only for the
#: case where the winning transaction rolled back or the row was deleted between
#: our INSERT and our SELECT.
_MAX_CREATE_ATTEMPTS = 2


def _violated_constraint(exc: IntegrityError) -> str | None:
    """Name the unique constraint an IntegrityError violated, if it names one.

    asyncpg raises `UniqueViolationError` with a `constraint_name` attribute.
    Returns None when the driver did not name one — `_is_race_violation` then
    falls back to the message text, because a wrapped or re-raised error may not
    carry the attribute and silently declining to retry is the failure mode this
    whole workstream exists to remove.
    """
    named = getattr(exc.orig, "constraint_name", None)
    if isinstance(named, str):
        return named
    return None


def _is_race_violation(exc: IntegrityError, constraints: Sequence[str]) -> bool:
    name = _violated_constraint(exc)
    if name is not None:
        return name in constraints
    text = str(exc)
    return any(c in text for c in constraints)


async def retry_on_unique_violation[T](
    session: AsyncSession,
    operation: Callable[[], Awaitable[T]],
    *,
    constraints: Sequence[str] = WIKI_RACE_CONSTRAINTS,
    attempts: int = MAX_COMMIT_ATTEMPTS,
) -> T:
    """Run `operation` inside a SAVEPOINT and retry it a BOUNDED number of times
    if it loses a race to one of `constraints`.

    Belt and braces behind the row lock in `_lock_or_create_page`, not a
    substitute for it. It exists because the cost of being wrong about the lock
    is a caller's work discarded as an unhandled 500, and because the lock does
    nothing for a future call site that reaches these tables another way.

    The SAVEPOINT is load-bearing rather than decorative: in Postgres the first
    failed statement aborts the whole transaction, so without one there is
    nothing left to retry *into* — every later statement would fail with
    "current transaction is aborted". Rolling back to the savepoint also leaves
    the caller's outer transaction usable when the budget is spent, which is why
    exhaustion can raise a 409 instead of poisoning the request.

    Raises `Conflict` when every attempt lost. `Conflict` and not the raw
    IntegrityError because 409 is the true answer — the work was not written and
    resubmitting it will succeed — where the 500 this replaces told the caller
    the server was broken and their commit was gone.
    """
    if attempts < 1:
        msg = "attempts must be at least 1"
        raise ValueError(msg)
    last: IntegrityError | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with session.begin_nested():
                return await operation()
        except IntegrityError as exc:
            if not _is_race_violation(exc, constraints):
                raise
            last = exc
            if attempt >= attempts:
                break
    msg = (
        f"wiki write lost a unique-constraint race {attempts} times "
        f"({_violated_constraint(last) if last else 'unknown'}); nothing was written"
    )
    raise Conflict(msg) from last


def _slugify(s: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return slug[:128] or "page"


def _split_sections(body_md: str) -> list[SectionDraft]:
    """Return SectionDraft entries from a markdown body using ATX headings as anchors."""
    sections: list[SectionDraft] = []
    matches = list(_HEADING.finditer(body_md))
    if not matches:
        return [
            SectionDraft(
                anchor="content",
                char_start=0,
                char_end=len(body_md),
                ordinal=0,
            )
        ]
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body_md)
        anchor = _slugify(m.group(2))
        sections.append(
            SectionDraft(
                anchor=anchor or f"section-{i}",
                char_start=start,
                char_end=end,
                ordinal=i,
            )
        )
    return sections


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _splice_protected_sections(
    new_body: str,
    *,
    prior_body: str | None,
    prior_sections: dict[str, tuple[int, int]],
    protected_anchors: set[str],
) -> str:
    """Replace each protected section in `new_body` with the corresponding text
    from `prior_body`. Anchor identity is by heading slug.
    """
    if not protected_anchors or prior_body is None:
        return new_body
    new_sections = _split_sections(new_body)
    parts: list[str] = []
    cursor = 0
    for sec in new_sections:
        parts.append(new_body[cursor : sec.char_start])
        if sec.anchor in protected_anchors and sec.anchor in prior_sections:
            ps, pe = prior_sections[sec.anchor]
            parts.append(prior_body[ps:pe])
        else:
            parts.append(new_body[sec.char_start : sec.char_end])
        cursor = sec.char_end
    parts.append(new_body[cursor:])
    return "".join(parts)


class WikiService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        index_service: IndexService | None = None,
    ) -> None:
        self._session = session
        self._index = index_service or IndexService(session)

    async def commit_revision(
        self,
        *,
        principal: Principal,
        ledger: LedgerWriter,
        project_id: UUID,
        page_id: UUID | None,
        title: str | None,
        slug: str | None,
        page_kind: PageKind,
        body_md: str,
        summary: str,
        claims: list[ClaimDraft],
        wikilinks: list[WikiLinkDraft],
        commit_message: str,
        respect_hand_edits: bool = True,
        origin: str = "agent",
    ) -> CommitResult:
        with start_span(
            "wiki.commit_revision",
            **{
                "aleph.project_id": str(project_id),
                "aleph.page_id": str(page_id) if page_id else "",
                "aleph.page_kind": page_kind,
            },
        ) as span:
            # One attempt at the whole commit, wrapped rather than inlined so that a
            # lost race rolls back to a SAVEPOINT and runs again cleanly. Every write
            # below — ledger event, revision, sections, links, claims, page pointer —
            # has to be undone together, and Postgres aborts the whole transaction on
            # the first failed statement, so there would otherwise be nothing left to
            # retry into.
            async def _attempt() -> CommitResult:
                page = await self._lock_or_create_page(
                    project_id=project_id,
                    page_id=page_id,
                    title=title,
                    slug=slug,
                    page_kind=page_kind,
                    created_by=principal.user_id,
                )

                prior_body, prior_sections = await self._prior_body_and_sections(page)
                effective_body = body_md
                if respect_hand_edits and prior_body is not None:
                    protected = {
                        m.section_anchor
                        for m in await list_active_for_page(
                            self._session, project_id=project_id, page_id=page.id
                        )
                        if m.section_anchor is not None
                    }
                    effective_body = _splice_protected_sections(
                        body_md,
                        prior_body=prior_body,
                        prior_sections=prior_sections,
                        protected_anchors=protected,
                    )

                body_hash = _hash(effective_body)
                # Idempotent no-op when the body hasn't changed.
                if page.current_revision_id is not None:
                    cur_rev = (
                        await self._session.execute(
                            select(WikiRevision).where(WikiRevision.id == page.current_revision_id)
                        )
                    ).scalar_one_or_none()
                    if cur_rev is not None and cur_rev.body_sha256 == body_hash:
                        span.set_attribute("aleph.wiki.noop", True)
                        return CommitResult(
                            page_id=page.id,
                            revision_id=cur_rev.id,
                            revision_no=cur_rev.revision_no,
                            body_sha256=body_hash,
                            was_noop=True,
                        )

                # Compute new revision number.
                max_rev = (
                    await self._session.execute(
                        select(WikiRevision.revision_no)
                        .where(WikiRevision.page_id == page.id)
                        .order_by(WikiRevision.revision_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                new_rev_no = (max_rev or 0) + 1

                # Append ledger entry first to capture the event id we'll write on the row.
                ledger_event = await ledger.append(
                    project_id=project_id,
                    actor_id=principal.user_id,
                    actor_kind=principal.actor_kind,
                    action_kind="wiki.revision.commit",
                    target_id=page.id,
                    target_kind="wiki_page",
                    payload={
                        "page_id": str(page.id),
                        "page_title": title,
                        "revision_no": new_rev_no,
                        "body_sha256": body_hash,
                        "page_kind": page_kind,
                        "commit_message": commit_message,
                        "origin": origin,
                    },
                    trace_id=current_trace_id(),
                )

                revision_id = uuid7()
                revision = WikiRevision(
                    id=revision_id,
                    page_id=page.id,
                    project_id=project_id,
                    revision_no=new_rev_no,
                    body_md=effective_body,
                    summary=summary[:2048],
                    author_kind=principal.actor_kind,
                    author_id=principal.user_id,
                    parent_revision_id=page.current_revision_id,
                    body_sha256=body_hash,
                    commit_message=commit_message[:2048],
                    trace_id=current_trace_id(),
                    ledger_event_id=ledger_event.id,
                )
                self._session.add(revision)
                await self._session.flush()

                # Replace WikiSection rows for the new revision.
                for sec in _split_sections(effective_body):
                    section_text = effective_body[sec.char_start : sec.char_end]
                    self._session.add(
                        WikiSection(
                            id=uuid7(),
                            project_id=project_id,
                            page_id=page.id,
                            revision_id=revision_id,
                            anchor=sec.anchor,
                            char_start=sec.char_start,
                            char_end=sec.char_end,
                            body_sha256=_hash(section_text),
                            ordinal=sec.ordinal,
                        )
                    )

                # Replace WikiLink rows for src_revision_id with the new ones.
                await self._session.execute(
                    delete(WikiLink).where(WikiLink.src_revision_id == revision_id)
                )
                for link in wikilinks:
                    self._session.add(
                        WikiLink(
                            id=uuid7(),
                            project_id=project_id,
                            src_page_id=page.id,
                            src_revision_id=revision_id,
                            dst_page_id=link.dst_page_id,
                            dst_title=link.dst_title,
                            occurrences=link.occurrences,
                        )
                    )

                # Insert claims + their citations.
                for c in claims:
                    claim = WikiClaim(
                        id=uuid7(),
                        project_id=project_id,
                        page_id=page.id,
                        revision_id=revision_id,
                        section_anchor=c.section_anchor,
                        text=c.text[:2048],
                        confidence=c.confidence,
                        status="active",
                        created_by=principal.user_id,
                    )
                    self._session.add(claim)
                    await self._session.flush()
                    for cite in c.citations:
                        self._session.add(
                            Citation(
                                id=uuid7(),
                                project_id=project_id,
                                claim_id=claim.id,
                                chunk_ids=[str(cid) for cid in cite.chunk_ids],
                                source_id=cite.source_id,
                                source_page_id=cite.source_page_id,
                                citation_marker=cite.citation_marker[:16],
                            )
                        )

                page.current_revision_id = revision_id
                page.last_compiled_at = utcnow()
                page.is_stub = page_kind == "stub"
                await self._session.flush()

                # Refresh WikiIndex row.
                await self._index.refresh_page(
                    project_id=project_id, page_id=page.id, summary=summary
                )

                return CommitResult(
                    page_id=page.id,
                    revision_id=revision_id,
                    revision_no=new_rev_no,
                    body_sha256=body_hash,
                    was_noop=False,
                )

            return await retry_on_unique_violation(self._session, _attempt)

    # ---- queries -----------------------------------------------------------

    async def get_page(
        self, *, project_id: UUID, page_id: UUID, revision_id: UUID | None = None
    ) -> tuple[WikiPage, WikiRevision] | None:
        page = (
            await self._session.execute(
                select(WikiPage).where(WikiPage.id == page_id, WikiPage.project_id == project_id)
            )
        ).scalar_one_or_none()
        if page is None:
            return None
        rev_id = revision_id or page.current_revision_id
        if rev_id is None:
            return (page, None)  # type: ignore[return-value]
        rev = (
            await self._session.execute(select(WikiRevision).where(WikiRevision.id == rev_id))
        ).scalar_one_or_none()
        return (page, rev) if rev is not None else None

    async def list_pages(self, *, project_id: UUID, kind: str | None = None) -> list[WikiPage]:
        stmt = (
            select(WikiPage)
            .where(WikiPage.project_id == project_id)
            .order_by(WikiPage.last_compiled_at.desc().nullslast(), WikiPage.title)
        )
        if kind:
            stmt = stmt.where(WikiPage.page_kind == kind)
        return list((await self._session.execute(stmt)).scalars().all())

    async def page_by_slug(self, *, project_id: UUID, slug: str) -> WikiPage | None:
        return (
            await self._session.execute(
                select(WikiPage).where(WikiPage.project_id == project_id, WikiPage.slug == slug)
            )
        ).scalar_one_or_none()

    # ---- internals ---------------------------------------------------------

    async def _lock_or_create_page(
        self,
        *,
        project_id: UUID,
        page_id: UUID | None,
        title: str | None,
        slug: str | None,
        page_kind: PageKind,
        created_by: UUID,
    ) -> WikiPage:
        if page_id is not None:
            page = (
                await self._session.execute(
                    select(WikiPage)
                    .where(
                        WikiPage.id == page_id,
                        WikiPage.project_id == project_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if page is None:
                msg = f"wiki page {page_id} not found"
                raise NotFound(msg)
            return page

        if not title:
            msg = "title is required when page_id is not provided"
            raise ValueError(msg)
        page_slug = slug or _slugify(title)

        # Create-or-lock in one atomic step, because doing it in two lost both
        # races that this function used to lose.
        #
        # What was here: a plain SELECT on (project_id, slug) that returned the
        # row UNLOCKED, then an ORM insert if it found nothing. Two failures fell
        # out of that, both on the agent path. Counted in the tree: four call
        # sites pass a literal `page_id=None` (synthesis_workflow, routes/notes,
        # routes/wiki's hub commit, jobs/bootstrap), and the wiki ingest
        # workflow passes a `UUID | None` that is None for any page it is
        # minting — so concurrent ingest of two sources that name the same topic
        # races every time:
        #
        #   1. The unlocked row let `commit_revision` compute
        #      `revision_no = max + 1` with nothing held. Two commits to the same
        #      page both read max=N, both inserted N+1, and the loser got an
        #      unhandled IntegrityError on uq_wiki_rev_page_no — a 500 with the
        #      caller's work discarded.
        #   2. Two commits minting the same NEW title both saw "does not exist"
        #      and collided on uq_wiki_pages_project_slug.
        #
        # ON CONFLICT DO NOTHING closes both at once. Measured against the
        # compose Postgres rather than assumed: when a concurrent transaction has
        # inserted this (project_id, slug) and not yet committed, this INSERT
        # BLOCKS until that transaction finishes instead of returning
        # immediately. So by the time the next statement runs the winner is
        # committed, the follow-up SELECT's fresh READ COMMITTED snapshot sees
        # it, and FOR UPDATE queues behind whoever holds the row. If the winner
        # rolled back instead, our INSERT is the one that lands.
        #
        # The FOR UPDATE is the half that makes `revision_no = max + 1` safe. Do
        # not "simplify" it away: without it this function is back to handing out
        # unlocked rows, and `scripts/check-page-lock.sh` fails on exactly that.
        page_values = {
            "id": uuid7(),
            "project_id": project_id,
            "title": title,
            "slug": page_slug,
            "page_kind": page_kind,
            "current_revision_id": None,
            "is_stub": page_kind == "stub",
            # A stub is not a draft, and must not enter the review queue.
            #
            # Stubs are minted whenever a page links to a title that does not
            # exist yet — they hold no content and nobody proposed them. Filing
            # them as "draft" put 235 empty placeholders in front of the owner
            # for approval alongside 15 real pages, which is 94% noise. An
            # approval gesture that has to be performed 235 times cannot mean
            # "I read this and agree"; it can only mean "make the banner go
            # away", which is worse than not asking.
            #
            # The reference design (hermes-agent's llm-wiki skill, which built
            # ~/wiki/ai-research) treats these as RED LINKS: allowed to be
            # unresolved until enough separate pages cite them.
            # `promote_stub_if_earned` below is that rule, and it moves them to
            # `planned` (a writing queue), never to `draft` (a review queue).
            "status": (schema_mod.STUB_STATUS if page_kind == "stub" else schema_mod.REVIEW_QUEUE),
            "last_compiled_at": None,
            "created_by": created_by,
        }
        for _ in range(_MAX_CREATE_ATTEMPTS):
            # RETURNING id says which branch we took: a row means we created the
            # page, no row means somebody else already had. It is not decoration
            # — it separates "lost the race, look again" from "wrote a row and
            # cannot read it back", and only the first of those is worth a retry.
            inserted_id = (
                await self._session.execute(
                    pg_insert(WikiPage)
                    .values(page_values)
                    .on_conflict_do_nothing(constraint="uq_wiki_pages_project_slug")
                    .returning(WikiPage.__table__.c.id)
                )
            ).scalar_one_or_none()
            page = (
                await self._session.execute(
                    select(WikiPage)
                    .where(
                        WikiPage.project_id == project_id,
                        WikiPage.slug == page_slug,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if page is not None:
                return page
            if inserted_id is not None:
                # We inserted the row and then could not read it back inside our
                # own transaction. That is not a race — it is a broken
                # assumption, and retrying would run it again and hide it.
                msg = (
                    f"inserted wiki page {inserted_id} ('{page_slug}') and could not "
                    "read it back in the same transaction"
                )
                raise Conflict(msg)
            # No row and no insert: the winner of the create race rolled back, or
            # something deleted the page between our INSERT and our SELECT. Try
            # once more — bounded, because a loop here is a livelock under fan-out.

        msg = (
            f"could not create or lock wiki page '{page_slug}' in project {project_id} "
            f"after {_MAX_CREATE_ATTEMPTS} attempts"
        )
        raise Conflict(msg)

    #: A stub becomes a real page when this many distinct sources mention it.
    #: One mention is a passing reference; two is a topic the corpus keeps
    #: returning to. Taken from the reference wiki's Page Thresholds.
    async def promote_stub_if_earned(
        self, *, project_id: UUID, page_id: UUID, threshold: int | None = None
    ) -> bool:
        """Move a red link into the writing queue once the corpus earns it.

        Creating a page for every mention is what produced hundreds of empty
        stubs from eleven sources. Creating one only when enough separate pages
        cite it moves the decision from the owner's queue, where it is hundreds
        of clicks, to ingest, where it is arithmetic.

        Promotion lands on `planned` — the 🚧 state — and NOT on `draft`.
        `draft` is the review queue, and "approve this" is not a question you
        can ask about a page with no content; that mistake is what put 235 empty
        pages in front of an approver. `planned` is a queue for *writing*, which
        is allowed to be long.

        `is_stub` stays true until something actually writes a body. The page
        has earned attention, not acquired content, and clearing the flag here
        would make it indistinguishable from a written page in every count.

        Returns True if the page moved.
        """
        page = (
            await self._session.execute(
                select(WikiPage).where(WikiPage.id == page_id, WikiPage.project_id == project_id)
            )
        ).scalar_one_or_none()
        if page is None or not page.is_stub or page.status == schema_mod.WRITING_QUEUE:
            return False

        # Distinct source PAGES, not distinct revisions. Counting revisions lets
        # one page that was edited five times clear a threshold of five on its
        # own, which measures editing activity rather than how many separate
        # places in the corpus needed this topic to exist.
        mentions = (
            await self._session.execute(
                select(func.count(func.distinct(WikiLink.src_page_id))).where(
                    WikiLink.project_id == project_id,
                    WikiLink.dst_page_id == page_id,
                )
            )
        ).scalar_one()

        bar = threshold if threshold is not None else schema_mod.STUB_PROMOTION_MENTIONS
        if (mentions or 0) < bar:
            return False

        page.status = schema_mod.WRITING_QUEUE
        await self._session.flush()
        return True

    async def _prior_body_and_sections(
        self, page: WikiPage
    ) -> tuple[str | None, dict[str, tuple[int, int]]]:
        if page.current_revision_id is None:
            return None, {}
        cur = (
            await self._session.execute(
                select(WikiRevision).where(WikiRevision.id == page.current_revision_id)
            )
        ).scalar_one_or_none()
        if cur is None:
            return None, {}
        sec_rows = list(
            (
                await self._session.execute(
                    select(WikiSection).where(WikiSection.revision_id == cur.id)
                )
            )
            .scalars()
            .all()
        )
        return cur.body_md, {s.anchor: (s.char_start, s.char_end) for s in sec_rows}
