"""Alias management. Extracted by the wiki agent; consumed by wikilink resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, update

from aleph_core.ids import uuid7
from aleph_wiki.models import Alias, WikiLink, WikiPage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AliasResolution:
    canonical_name: str
    canonical_page_id: UUID | None


class AliasService:
    def __init__(self, session: "AsyncSession") -> None:
        self._session = session

    async def upsert(
        self,
        *,
        project_id: UUID,
        surface_form: str,
        canonical_name: str,
        canonical_page_id: UUID | None = None,
        confidence: float = 1.0,
        created_by: UUID,
    ) -> Alias:
        existing = (
            await self._session.execute(
                select(Alias).where(
                    Alias.project_id == project_id,
                    Alias.surface_form == surface_form,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.canonical_name = canonical_name
            existing.canonical_page_id = canonical_page_id
            existing.confidence = max(existing.confidence, confidence)
            await self._session.flush()
            return existing
        a = Alias(
            id=uuid7(),
            project_id=project_id,
            surface_form=surface_form[:512],
            canonical_name=canonical_name[:512],
            canonical_page_id=canonical_page_id,
            confidence=confidence,
            created_by=created_by,
            access_scope="project",
        )
        self._session.add(a)
        await self._session.flush()
        return a

    async def resolve(
        self, *, project_id: UUID, surface_form: str
    ) -> AliasResolution | None:
        a = (
            await self._session.execute(
                select(Alias).where(
                    Alias.project_id == project_id,
                    Alias.surface_form == surface_form,
                )
            )
        ).scalar_one_or_none()
        if a is None:
            # Fall back to title-match against WikiPage.
            page = (
                await self._session.execute(
                    select(WikiPage).where(
                        WikiPage.project_id == project_id,
                        WikiPage.title == surface_form,
                    )
                )
            ).scalar_one_or_none()
            if page is None:
                return None
            return AliasResolution(canonical_name=page.title, canonical_page_id=page.id)
        return AliasResolution(
            canonical_name=a.canonical_name, canonical_page_id=a.canonical_page_id
        )

    async def repair_broken_links(self, *, project_id: UUID) -> int:
        """Iterate WikiLink rows with null dst_page_id; try to resolve via aliases.
        Returns the number of links repaired.
        """
        rows = list(
            (
                await self._session.execute(
                    select(WikiLink).where(
                        WikiLink.project_id == project_id,
                        WikiLink.dst_page_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        n_repaired = 0
        for link in rows:
            r = await self.resolve(project_id=project_id, surface_form=link.dst_title)
            if r and r.canonical_page_id:
                link.dst_page_id = r.canonical_page_id
                n_repaired += 1
        await self._session.flush()
        return n_repaired
