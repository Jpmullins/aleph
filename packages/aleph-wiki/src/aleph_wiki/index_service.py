"""WikiIndex maintenance — denormalized retrieval index per WikiPage.

The trigger on `wiki_index` regenerates `index_tsv` from `title + summary
+ aliases_jsonb` on every insert/update. `refresh_page` and
`select_pages` are the only paths to that table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from aleph_core.time import utcnow
from aleph_wiki.models import Alias, WikiIndex, WikiLink, WikiPage, WikiRevision

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PageSelectionResult:
    page_id: UUID
    title: str
    slug: str
    summary: str
    score: float
    wikilinks_out: list[dict]
    page_kind: str
    is_stub: bool


class IndexService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def refresh_page(
        self,
        *,
        project_id: UUID,
        page_id: UUID,
        summary: str | None = None,
    ) -> None:
        page = (
            await self._session.execute(select(WikiPage).where(WikiPage.id == page_id))
        ).scalar_one_or_none()
        if page is None:
            return
        # Aliases pointing at this page.
        alias_rows = list(
            (
                await self._session.execute(
                    select(Alias.surface_form).where(
                        Alias.project_id == project_id,
                        (Alias.canonical_page_id == page_id) | (Alias.canonical_name == page.title),
                    )
                )
            )
            .scalars()
            .all()
        )
        # Outgoing wikilinks (current revision).
        wikilinks: list[dict] = []
        if page.current_revision_id is not None:
            link_rows = list(
                (
                    await self._session.execute(
                        select(WikiLink).where(WikiLink.src_revision_id == page.current_revision_id)
                    )
                )
                .scalars()
                .all()
            )
            wikilinks = [
                {
                    "dst_title": link.dst_title,
                    "dst_page_id": str(link.dst_page_id) if link.dst_page_id else None,
                    "occurrences": link.occurrences,
                }
                for link in link_rows
            ]

        # Resolve summary: prefer caller-supplied, else fall back to current revision.summary.
        if summary is None and page.current_revision_id is not None:
            cur = (
                await self._session.execute(
                    select(WikiRevision).where(WikiRevision.id == page.current_revision_id)
                )
            ).scalar_one_or_none()
            summary = (cur.summary if cur else "") or ""
        summary = (summary or "")[:2048]

        # Upsert. index_tsv is filled by the trigger.
        stmt = insert(WikiIndex).values(
            page_id=page.id,
            project_id=project_id,
            title=page.title,
            slug=page.slug,
            aliases_jsonb=alias_rows,
            summary=summary,
            wikilinks_out_jsonb=wikilinks,
            page_kind=page.page_kind,
            status=page.status,
            is_stub=page.is_stub,
            indexed_at=utcnow(),
            index_tsv="",  # trigger overrides
        )
        update_cols = {
            "title": stmt.excluded.title,
            "slug": stmt.excluded.slug,
            "aliases_jsonb": stmt.excluded.aliases_jsonb,
            "summary": stmt.excluded.summary,
            "wikilinks_out_jsonb": stmt.excluded.wikilinks_out_jsonb,
            "page_kind": stmt.excluded.page_kind,
            "status": stmt.excluded.status,
            "is_stub": stmt.excluded.is_stub,
            "indexed_at": stmt.excluded.indexed_at,
        }
        await self._session.execute(
            stmt.on_conflict_do_update(index_elements=["page_id"], set_=update_cols)
        )

    async def list_pages(self, *, project_id: UUID, top_k: int = 8) -> list[PageSelectionResult]:
        """List a project's indexed pages without a query.

        Used as a fallback when a keyword search yields no FTS hits: a generic
        or conceptual query ("what is this project about") shares no tokens with
        any title/summary, so `select_pages` returns empty even when a wiki
        exists. Returning the real pages (substantive first, then most recently
        indexed) lets a caller see the wiki rather than conclude it is empty.
        """
        stmt = (
            select(
                WikiIndex.page_id,
                WikiIndex.title,
                WikiIndex.slug,
                WikiIndex.summary,
                WikiIndex.page_kind,
                WikiIndex.is_stub,
                WikiIndex.wikilinks_out_jsonb,
            )
            .where(WikiIndex.project_id == project_id)
            .order_by(WikiIndex.is_stub.asc(), WikiIndex.indexed_at.desc())
            .limit(top_k)
        )
        rows = list((await self._session.execute(stmt)).all())
        return [
            PageSelectionResult(
                page_id=row.page_id,
                title=row.title,
                slug=row.slug,
                summary=row.summary,
                score=0.0,
                wikilinks_out=row.wikilinks_out_jsonb,
                page_kind=row.page_kind,
                is_stub=row.is_stub,
            )
            for row in rows
        ]

    async def select_pages(
        self, *, project_id: UUID, query: str, top_k: int = 8
    ) -> list[PageSelectionResult]:
        stmt = (
            select(
                WikiIndex.page_id,
                WikiIndex.title,
                WikiIndex.slug,
                WikiIndex.summary,
                WikiIndex.page_kind,
                WikiIndex.is_stub,
                WikiIndex.wikilinks_out_jsonb,
                func.ts_rank(
                    WikiIndex.index_tsv,
                    func.plainto_tsquery("english", query),
                ).label("rank"),
            )
            .where(
                WikiIndex.project_id == project_id,
                WikiIndex.index_tsv.op("@@")(func.plainto_tsquery("english", query)),
            )
            .order_by(text("rank DESC"))
            .limit(top_k)
        )
        rows = list((await self._session.execute(stmt)).all())
        return [
            PageSelectionResult(
                page_id=row.page_id,
                title=row.title,
                slug=row.slug,
                summary=row.summary,
                score=float(row.rank),
                wikilinks_out=row.wikilinks_out_jsonb,
                page_kind=row.page_kind,
                is_stub=row.is_stub,
            )
            for row in rows
        ]
