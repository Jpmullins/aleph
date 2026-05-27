"""Wiki service: the only path that writes WikiPage / WikiRevision.

`commit_revision` is atomic: lock page, apply HandEditMark protection,
compute body_sha256, no-op if unchanged, insert new revision, replace
sections, replace wikilinks, insert claims + citations, update the page
pointer, refresh WikiIndex, append ledger event.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import delete, select, update

from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id, start_span
from aleph_wiki.handedit_service import list_active_for_page
from aleph_wiki.index_service import IndexService
from aleph_wiki.models import (
    Citation,
    HandEditMark,
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
    citations: list["CitationDraft"] = field(default_factory=list)


@dataclass(frozen=True)
class CitationDraft:
    chunk_ids: list[UUID]
    source_page_id: UUID | None
    citation_marker: str


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
        session: "AsyncSession",
        *,
        index_service: IndexService | None = None,
    ) -> None:
        self._session = session
        self._index = index_service or IndexService(session)

    async def commit_revision(
        self,
        *,
        principal: "Principal",
        ledger: "LedgerWriter",
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
    ) -> CommitResult:
        with start_span(
            "wiki.commit_revision",
            **{
                "aleph.project_id": str(project_id),
                "aleph.page_id": str(page_id) if page_id else "",
                "aleph.page_kind": page_kind,
            },
        ) as span:
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
                        select(WikiRevision).where(
                            WikiRevision.id == page.current_revision_id
                        )
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
                    "revision_no": new_rev_no,
                    "body_sha256": body_hash,
                    "page_kind": page_kind,
                    "commit_message": commit_message,
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
                    access_scope="project",
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

    # ---- queries -----------------------------------------------------------

    async def get_page(
        self, *, project_id: UUID, page_id: UUID, revision_id: UUID | None = None
    ) -> tuple[WikiPage, WikiRevision] | None:
        page = (
            await self._session.execute(
                select(WikiPage).where(
                    WikiPage.id == page_id, WikiPage.project_id == project_id
                )
            )
        ).scalar_one_or_none()
        if page is None:
            return None
        rev_id = revision_id or page.current_revision_id
        if rev_id is None:
            return (page, None)  # type: ignore[return-value]
        rev = (
            await self._session.execute(
                select(WikiRevision).where(WikiRevision.id == rev_id)
            )
        ).scalar_one_or_none()
        return (page, rev) if rev is not None else None

    async def list_pages(
        self, *, project_id: UUID, kind: str | None = None
    ) -> list[WikiPage]:
        stmt = (
            select(WikiPage)
            .where(WikiPage.project_id == project_id)
            .order_by(WikiPage.last_compiled_at.desc().nullslast(), WikiPage.title)
        )
        if kind:
            stmt = stmt.where(WikiPage.page_kind == kind)
        return list((await self._session.execute(stmt)).scalars().all())

    async def page_by_slug(
        self, *, project_id: UUID, slug: str
    ) -> WikiPage | None:
        return (
            await self._session.execute(
                select(WikiPage).where(
                    WikiPage.project_id == project_id, WikiPage.slug == slug
                )
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
        existing = (
            await self._session.execute(
                select(WikiPage).where(
                    WikiPage.project_id == project_id,
                    WikiPage.slug == page_slug,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        new_page = WikiPage(
            id=uuid7(),
            project_id=project_id,
            title=title,
            slug=page_slug,
            page_kind=page_kind,
            current_revision_id=None,
            is_stub=page_kind == "stub",
            status="draft",
            last_compiled_at=None,
            created_by=created_by,
            access_scope="project",
        )
        self._session.add(new_page)
        await self._session.flush()
        return new_page

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
