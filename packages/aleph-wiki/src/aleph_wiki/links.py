"""Resolve `[[wikilinks]]` to the pages they mean.

A link is stored as the text the author wrote (`dst_title`) plus a resolved
`dst_page_id`, and the resolved half is filled in after the fact. The only
resolver that existed went through the legacy alias table, so a link resolved
only if somebody had recorded an alias for it — an exact title match did not
resolve, and neither did a slug.

That is not a hypothetical gap. On the live corpus, every source page is titled
`Source: How to Write to SSDs` with slug `source-s0002`, while the compiler
emits `[[Source:S0002]]` — the slug form. 396 link rows across 11 source pages
pointed at nothing, and the lint reported each as broken, because the resolver
had no way to see that `Source:S0002` and `source-s0002` are the same page.

Slug resolution is not a workaround for that; it is the documented behaviour.
The vault schema says slugs are globally unique so `[[slug]]` resolves wherever
the file lives, which is how Obsidian's shortest-path linking works.

Order matters and is deliberate: exact title, then case-insensitive title, then
slug. A page whose title exactly matches wins over one whose slug happens to
normalise the same way, because the author wrote a title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_wiki.models import WikiLink, WikiPage

__all__ = ["LinkRepair", "resolve_broken_links", "slugify_link"]

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify_link(text: str) -> str:
    """Normalise link text the way a slug is built.

    `Source:S0002` → `source-s0002`, `Write-Ahead Log` → `write-ahead-log`.
    Deliberately the same shape as the slug column so the comparison is exact
    rather than fuzzy — this resolves links, and a fuzzy match here would
    silently point a citation at the wrong source.
    """
    return _NON_SLUG.sub("-", text.strip().lower()).strip("-")


@dataclass(frozen=True, slots=True)
class LinkRepair:
    resolved: int
    still_broken: int
    by_title: int
    by_slug: int

    def summary(self) -> str:
        return (
            f"{self.resolved} links resolved ({self.by_title} by title, "
            f"{self.by_slug} by slug); {self.still_broken} still point at "
            "pages that do not exist"
        )


async def resolve_broken_links(
    session: AsyncSession, *, project_id: UUID, dry_run: bool = False
) -> LinkRepair:
    """Point every resolvable broken link at its page.

    A link left unresolved after this genuinely names a page that does not
    exist — which is the useful reading, and what makes the lint's
    `broken-wikilink` count mean something. Before this, that count conflated
    "nobody wrote this page" with "the resolver could not see the page that was
    right there".

    `dry_run` reports what would resolve without writing, so a caller can show
    the number before spending it.
    """
    pages = (
        await session.execute(
            select(WikiPage.id, WikiPage.title, WikiPage.slug).where(
                WikiPage.project_id == project_id
            )
        )
    ).all()

    by_title: dict[str, UUID] = {}
    by_title_ci: dict[str, UUID] = {}
    by_slug: dict[str, UUID] = {}
    for page_id, title, slug in pages:
        by_title.setdefault(title, page_id)
        by_title_ci.setdefault(title.lower(), page_id)
        by_slug.setdefault(slug, page_id)

    broken = list(
        (
            await session.execute(
                select(WikiLink).where(
                    WikiLink.project_id == project_id, WikiLink.dst_page_id.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )

    title_hits = slug_hits = 0
    for link in broken:
        text = link.dst_title
        target = by_title.get(text) or by_title_ci.get(text.lower())
        matched_by = "title"
        if target is None:
            target = by_slug.get(slugify_link(text))
            matched_by = "slug"
        if target is None:
            continue
        # A link must never point at its own page. A self-link is not
        # navigation, and it would make the page its own inbound link — which
        # is precisely what the orphan check reads, so every page with one
        # would stop being reported as unreachable while still being so.
        if target == link.src_page_id:
            continue
        if not dry_run:
            link.dst_page_id = target
        if matched_by == "title":
            title_hits += 1
        else:
            slug_hits += 1

    if not dry_run:
        await session.flush()

    resolved = title_hits + slug_hits
    return LinkRepair(
        resolved=resolved,
        still_broken=len(broken) - resolved,
        by_title=title_hits,
        by_slug=slug_hits,
    )
