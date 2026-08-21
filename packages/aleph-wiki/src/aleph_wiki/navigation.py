"""Category hubs and the project index — the vault's navigational backbone.

The hermes skill keeps two hand-maintained files: `_hub.md` per category, and
`index.md` listing every page under its section. It also names the failure that
follows from them being hand-maintained — "Always update index.md and log.md —
skipping this makes the wiki degrade" — which is a rule that holds exactly as
long as whoever writes a page remembers to.

Aleph derives both instead. A hub is a query over `category`; the index is a
query over `page_type` and `category`. There is nothing to forget, nothing to
drift, and a page that exists is in the index by construction rather than by
diligence.

The hub is still written out as a real page (`page_type="hub"`) rather than
computed only at render time, for two reasons: `[[architectures-hub]]` has to
resolve like any other wikilink, and an exported vault has to open in Obsidian
with its navigation intact. Regenerating is idempotent — an unchanged hub is
not rewritten, so a no-op regeneration does not manufacture revisions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_wiki.frontmatter import Frontmatter, render, strip
from aleph_wiki.models import WikiPage, WikiRevision
from aleph_wiki.schema import Category, WikiSchema

__all__ = [
    "HubPlan",
    "HubSyncResult",
    "IndexSection",
    "build_index",
    "plan_hubs",
    "render_hub",
    "sync_hubs",
]

#: What `sync_hubs` calls to persist one hub. Injected so this module stays a
#: pure query — writing a revision needs a principal and a ledger writer that
#: only the route has.
HubCommit = Callable[["HubPlan"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HubEntry:
    title: str
    slug: str
    summary: str
    status: str
    is_stub: bool


@dataclass(frozen=True, slots=True)
class HubPlan:
    """What a category's hub page should contain right now."""

    category: Category
    entries: tuple[HubEntry, ...]
    body_md: str


@dataclass(frozen=True, slots=True)
class IndexSection:
    key: str
    title: str
    entries: tuple[HubEntry, ...]


def _first_sentence(body_md: str, limit: int = 140) -> str:
    """A one-line summary lifted from the page itself.

    Taken from the body rather than stored separately because a stored summary
    is one more thing that goes stale silently — it would keep describing what
    the page said when someone last remembered to update it.
    """
    for raw in strip(body_md).splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "|", "-", "*", "!", "```")):
            continue
        # Strip the markdown emphasis that opens most definition sentences so
        # the hub reads as prose rather than as source.
        line = line.replace("**", "").replace("__", "")
        sentence = line.split(". ")[0].rstrip(".")
        return sentence[:limit] + ("…" if len(sentence) > limit else "")
    return ""


async def _entries_by_category(
    session: AsyncSession, project_id: UUID
) -> dict[str | None, list[HubEntry]]:
    rows = (
        await session.execute(
            select(
                WikiPage.title,
                WikiPage.slug,
                WikiPage.category,
                WikiPage.status,
                WikiPage.is_stub,
                WikiPage.page_type,
                WikiRevision.body_md,
            )
            .outerjoin(WikiRevision, WikiRevision.id == WikiPage.current_revision_id)
            .where(WikiPage.project_id == project_id)
            .order_by(WikiPage.title)
        )
    ).all()

    grouped: dict[str | None, list[HubEntry]] = {}
    for title, slug, category, status, is_stub, page_type, body in rows:
        # A hub does not list itself, and listing other hubs inside a hub turns
        # the top level into a loop rather than a tree.
        if page_type == "hub":
            continue
        grouped.setdefault(category, []).append(
            HubEntry(
                title=title,
                slug=slug,
                summary=_first_sentence(body or ""),
                status=status,
                is_stub=is_stub,
            )
        )
    return grouped


def render_hub(category: Category, entries: tuple[HubEntry, ...]) -> str:
    """The markdown body of one category hub.

    Written pages come first with their summaries; unwritten titles are listed
    below under the 🚧 marker the hermes index uses, because a hub that mixes
    them reads as though the category is three times larger than it is.
    """
    written = [e for e in entries if not e.is_stub]
    planned = [e for e in entries if e.is_stub]

    lines: list[str] = [f"# {category.title}", ""]
    if category.blurb:
        lines += [category.blurb + ".", ""]

    if written:
        # Inline links in a lead paragraph, so the hub satisfies the same
        # minimum-outbound-links rule every other page is held to instead of
        # being exempted from the structure it exists to provide.
        lead = ", ".join(f"[[{e.slug}|{e.title}]]" for e in written[:8])
        lines += [f"This category covers {lead}.", "", "## Pages", ""]
        lines += [
            f"- [[{e.slug}|{e.title}]]" + (f" — {e.summary}" if e.summary else "") for e in written
        ]
    else:
        lines += ["No pages have been written in this category yet.", ""]

    if planned:
        lines += [
            "",
            "## Planned",
            "",
            "Titles other pages link to that nobody has written yet.",
            "",
        ]
        lines += [f"- 🚧 [[{e.slug}|{e.title}]]" for e in planned]

    lines.append("")
    return "\n".join(lines)


async def plan_hubs(
    session: AsyncSession, *, project_id: UUID, schema: WikiSchema
) -> list[HubPlan]:
    """What every category hub should say, given the corpus as it stands.

    Returns a plan rather than writing: the write is a revision commit with a
    ledger event, and that belongs to the service. A category with no pages at
    all is skipped — an empty hub is a page that exists to say nothing.
    """
    grouped = await _entries_by_category(session, project_id)
    plans: list[HubPlan] = []
    for category in schema.categories:
        entries = tuple(grouped.get(category.id) or ())
        if not entries:
            continue
        plans.append(
            HubPlan(
                category=category,
                entries=entries,
                body_md=render(
                    Frontmatter(
                        title=f"{category.title} Hub",
                        type="hub",
                        category=category.id,
                        tags=["hub"],
                        related=[e.slug for e in entries[:3] if not e.is_stub],
                    ),
                    render_hub(category, entries),
                ),
            )
        )
    return plans


async def build_index(
    session: AsyncSession, *, project_id: UUID, schema: WikiSchema
) -> list[IndexSection]:
    """The project index, sectioned by category with an uncategorised tail.

    The tail is deliberate. The hermes index has no place for a page with no
    section, so such a page is simply absent — present in the vault, missing
    from the one document that is supposed to list everything. Showing it under
    "Uncategorised" makes the gap visible instead, which is also what the lint
    reports as `uncategorised`.
    """
    grouped = await _entries_by_category(session, project_id)
    sections: list[IndexSection] = []
    for category in schema.categories:
        entries = tuple(grouped.get(category.id) or ())
        if entries:
            sections.append(IndexSection(category.id, category.title, entries))

    known = schema.category_ids
    loose: list[HubEntry] = []
    for category_id, entries in grouped.items():
        if category_id is None or category_id not in known:
            loose.extend(entries)
    if loose:
        sections.append(
            IndexSection(
                "uncategorised",
                "Uncategorised",
                tuple(sorted(loose, key=lambda e: e.title)),
            )
        )
    return sections


@dataclass(frozen=True, slots=True)
class HubSyncResult:
    created: int
    updated: int
    unchanged: int

    def summary(self) -> str:
        return (
            f"{self.created} hubs created, {self.updated} updated, {self.unchanged} already current"
        )


async def sync_hubs(
    session: AsyncSession,
    *,
    project_id: UUID,
    schema: WikiSchema,
    commit: HubCommit,
) -> HubSyncResult:
    """Write every category hub, skipping the ones already correct.

    Idempotent by body hash. Without the skip, regenerating hubs on a schedule
    would append a revision per category per run — the revision table is
    immutable and append-only, so a no-op regeneration would permanently inflate
    the history of a page that did not change, and `freshness` would keep
    reporting hubs as the most recently edited pages in the wiki.

    `commit` is injected rather than the service imported, because writing a
    revision needs a principal and a ledger writer that only the caller has, and
    because it keeps this module a pure query over the corpus.
    """
    plans = await plan_hubs(session, project_id=project_id, schema=schema)
    created = updated = unchanged = 0

    existing = {
        slug: (page_id, body)
        for slug, page_id, body in (
            await session.execute(
                select(WikiPage.slug, WikiPage.id, WikiRevision.body_md)
                .outerjoin(WikiRevision, WikiRevision.id == WikiPage.current_revision_id)
                .where(WikiPage.project_id == project_id, WikiPage.page_type == "hub")
            )
        ).all()
    }

    for plan in plans:
        slug = plan.category.hub_slug
        current = existing.get(slug)
        if current is not None and (current[1] or "") == plan.body_md:
            unchanged += 1
            continue
        await commit(plan)
        if current is None:
            created += 1
        else:
            updated += 1

    return HubSyncResult(created=created, updated=updated, unchanged=unchanged)
