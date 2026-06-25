"""Wiki curator: knits the wiki graph after a page is committed.

Runs out-of-band (``curate_page_job``) after any authoring path commits a page.
Deterministic, idempotent steps:

1. ``register_aliases`` — register the new page's canonical title as an alias
   pointing at it, so links resolve through it.
2. ``repair_links`` — back-resolve every broken (``dst_page_id IS NULL``)
   wikilink in the project, so the overview and sibling pages link to the new
   page. (``AliasService.repair_broken_links``.)

This closes the gap that left wikilinks permanently broken: links resolved only
at write time, and the project-wide repair was never on the research/bootstrap
path (only the ingest agent + a manual button called it). See
``docs/superpowers/specs/2026-06-25-wiki-curator-design.md`` §1, §4.

The LLM curation steps (dedup→merge, incremental overview recuration) are
Slice 2 and are not part of this service yet; because this slice never commits
a revision, it cannot trigger a curation loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_observability.tracing import start_span
from aleph_wiki.alias_service import AliasService
from aleph_wiki.models import WikiPage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CurationResult:
    links_repaired: int
    aliases_registered: int


class CuratorService:
    """Deterministic graph-knitting run after a page commit.

    Mutates ``Alias`` and ``WikiLink.dst_page_id`` rows only — never commits a
    new revision — so it is safe to run repeatedly and cannot recurse.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._aliases = AliasService(session)

    async def curate(self, *, project_id: UUID, page_id: UUID) -> CurationResult:
        with start_span(
            "wiki.curate",
            **{"aleph.project_id": str(project_id), "aleph.page_id": str(page_id)},
        ):
            aliases_registered = await self._register_aliases(
                project_id=project_id, page_id=page_id
            )
            links_repaired = await self._repair_links(project_id=project_id)
            return CurationResult(
                links_repaired=links_repaired,
                aliases_registered=aliases_registered,
            )

    async def _register_aliases(self, *, project_id: UUID, page_id: UUID) -> int:
        with start_span("wiki.curate.register_aliases", **{"aleph.page_id": str(page_id)}):
            page = (
                await self._session.execute(
                    select(WikiPage).where(
                        WikiPage.id == page_id,
                        WikiPage.project_id == project_id,
                    )
                )
            ).scalar_one_or_none()
            if page is None:
                return 0
            await self._aliases.upsert(
                project_id=project_id,
                surface_form=page.title,
                canonical_name=page.title,
                canonical_page_id=page.id,
                created_by=page.created_by,
            )
            return 1

    async def _repair_links(self, *, project_id: UUID) -> int:
        with start_span("wiki.curate.repair_links", **{"aleph.project_id": str(project_id)}):
            return await self._aliases.repair_broken_links(project_id=project_id)
