"""Wiki curator: knits the wiki graph after a page is committed.

Runs out-of-band (``curate_page_job``) after any authoring path commits a page.

Deterministic steps (always; ``curate``):
1. ``register_aliases`` — register the new page's canonical title as an alias
   pointing at it, so links resolve through it.
2. ``repair_links`` — back-resolve every broken (``dst_page_id IS NULL``)
   wikilink in the project, so the overview and sibling pages link to the new
   page. (``AliasService.repair_broken_links``.)

LLM step (when the job supplies a gateway client; ``recurate_overview``):
3. fold the new topic into the project overview page — ensure a ``[[link]]``,
   add a short description, surface contradictions — as an incremental,
   hand-edit-respecting curator commit.

This closes the gap that left wikilinks permanently broken and the overview
stale after research: links resolved only at write time, and neither
``repair_broken_links`` nor any overview update was on the research/bootstrap
path. See ``docs/superpowers/specs/2026-06-25-wiki-curator-design.md`` §1, §4.

The curator commits the overview through ``WikiService.commit_revision`` with
``origin="curator"``. Curation is enqueued only at the authoring *job* sites
(wiki ingest + AIQ synthesis), never inside ``commit_revision``, so the
curator's own overview commit cannot re-trigger curation — no loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.schemas.model_profile import Capability
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.client import ChatMessage
from aleph_observability.tracing import start_span
from aleph_wiki.alias_service import AliasService
from aleph_wiki.models import WikiPage, WikiRevision
from aleph_wiki.wiki_service import WikiLinkDraft, WikiService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

_OVERVIEW_SYS = (
    "You maintain the overview/index page of a research wiki. You are given the "
    "current overview markdown and a newly-added topic page. Return an updated "
    "overview that:\n"
    "1. contains a `[[<topic title>]]` wikilink to the new page, using its EXACT "
    "title;\n"
    "2. adds or updates a one- to two-sentence description of the topic in the "
    "most relevant section (create a short section if none fits);\n"
    "3. preserves ALL existing content, sections, and wikilinks — only add or "
    "lightly revise;\n"
    "4. if the new topic contradicts an existing statement in the overview, note "
    "it briefly inline.\n"
    "Return ONLY the full updated overview markdown — no code fences, no "
    "commentary."
)


def _overview_user(overview_md: str, new_title: str, new_summary: str) -> str:
    return (
        f"Current overview markdown:\n\n{overview_md}\n\n---\n"
        f"New topic page title: {new_title}\n"
        f"New topic summary: {new_summary or '(no summary)'}"
    )


@dataclass(frozen=True)
class CurationResult:
    links_repaired: int
    aliases_registered: int


class CuratorService:
    """Graph-knitting + overview recuration run after a page commit.

    The deterministic ``curate`` mutates ``Alias`` and ``WikiLink.dst_page_id``
    rows only. ``recurate_overview`` commits an overview revision via
    ``WikiService`` (``origin="curator"``); it is a separate call so the
    deterministic path stays cheap and side-effect-light.
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
            page = await self._get_page(project_id=project_id, page_id=page_id)
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

    async def recurate_overview(
        self,
        *,
        project_id: UUID,
        new_page_id: UUID,
        litellm: LiteLLMClient,
        profile_bindings: dict[str, Any],
        principal: Principal,
        agent_run_id: UUID,
    ) -> bool:
        """Fold the new topic page into the project overview page.

        Identifies the overview as the page whose title matches the project
        title (the bootstrap-seeded overview). Returns True if it committed an
        updated overview revision, False if it skipped (no overview, the page
        *is* the overview, or no change).
        """
        with start_span(
            "wiki.curate.recurate_overview",
            **{"aleph.project_id": str(project_id), "aleph.page_id": str(new_page_id)},
        ):
            project = (
                await self._session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                return False
            overview = (
                await self._session.execute(
                    select(WikiPage).where(
                        WikiPage.project_id == project_id,
                        WikiPage.title == project.title,
                    )
                )
            ).scalar_one_or_none()
            if overview is None or overview.id == new_page_id:
                return False
            new_page = await self._get_page(project_id=project_id, page_id=new_page_id)
            if new_page is None:
                return False

            overview_rev = await self._current_revision(overview)
            if overview_rev is None:
                return False
            new_rev = await self._current_revision(new_page)
            new_summary = (new_rev.summary if new_rev else "") or ""

            resp = await litellm.chat(
                principal=principal,
                project_id=project_id,
                agent_run_id=agent_run_id,
                capability=Capability.SYNTHESIS,
                profile_bindings=profile_bindings,
                messages=[
                    ChatMessage(role="system", content=_OVERVIEW_SYS),
                    ChatMessage(
                        role="user",
                        content=_overview_user(overview_rev.body_md, new_page.title, new_summary),
                    ),
                ],
                temperature=0.2,
                max_tokens=4000,
                purpose="wiki.curate.overview",
            )
            updated = ((resp.choices[0].message.content if resp.choices else "") or "").strip()
            if not updated:
                return False
            # Belt-and-suspenders: guarantee the new link exists even if the
            # model omitted it.
            if f"[[{new_page.title}]]" not in updated:
                updated = f"{updated.rstrip()}\n\n- [[{new_page.title}]]\n"

            links = await self._resolve_body_links(project_id=project_id, body_md=updated)

            result = await WikiService(self._session).commit_revision(
                principal=principal,
                ledger=LedgerWriter(self._session),
                project_id=project_id,
                page_id=overview.id,
                title=overview.title,
                slug=overview.slug,
                page_kind="topic",
                body_md=updated,
                summary=(overview_rev.summary or "")[:2048],
                claims=[],
                wikilinks=links,
                commit_message=f"Curator: fold in [[{new_page.title}]]",
                respect_hand_edits=True,
                origin="curator",
            )
            return not result.was_noop

    async def _resolve_body_links(self, *, project_id: UUID, body_md: str) -> list[WikiLinkDraft]:
        counts: dict[str, int] = {}
        for m in _WIKILINK_RE.finditer(body_md):
            title = m.group(1).split("|", 1)[0].strip()
            if title:
                counts[title] = counts.get(title, 0) + 1
        links: list[WikiLinkDraft] = []
        for title, occ in counts.items():
            r = await self._aliases.resolve(project_id=project_id, surface_form=title)
            links.append(
                WikiLinkDraft(
                    dst_title=title,
                    dst_page_id=r.canonical_page_id if r else None,
                    occurrences=occ,
                )
            )
        return links

    async def _get_page(self, *, project_id: UUID, page_id: UUID) -> WikiPage | None:
        return (
            await self._session.execute(
                select(WikiPage).where(
                    WikiPage.id == page_id,
                    WikiPage.project_id == project_id,
                )
            )
        ).scalar_one_or_none()

    async def _current_revision(self, page: WikiPage) -> WikiRevision | None:
        if page.current_revision_id is None:
            return None
        return (
            await self._session.execute(
                select(WikiRevision).where(WikiRevision.id == page.current_revision_id)
            )
        ).scalar_one_or_none()
