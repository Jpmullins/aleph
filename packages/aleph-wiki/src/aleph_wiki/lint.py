"""Wiki lint — the checks that keep a corpus from quietly rotting.

A wiki degrades in ways no single write notices. One page links to a title
nobody wrote. One page nothing links to. One tag invented on the fly. One
contradiction resolved by whichever ingest ran last. Each is harmless; the
accumulation is a corpus that answers questions wrongly while every individual
operation reports success — the same shape as the four LangGraph defects
recorded in CLAUDE.md.

This implements the hermes-agent `llm-wiki` lint, adapted where Aleph's storage
makes a check mean something different (noted per check). It is read-only: it
reports, it never repairs. Repair is a write, and writes go through the service
so they land in the ledger.

Severity ordering follows the skill: broken links first, because they are the
only class that makes navigation actually fail, then structural problems, then
quality signals. The order is what a person reads top-down when they have ten
minutes, so it has to put the thing worth fixing first.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_wiki.frontmatter import extract_wikilinks, parse
from aleph_wiki.models import WikiLink, WikiPage, WikiRevision
from aleph_wiki.schema import EXEMPT_TYPES, WRITING_QUEUE, WikiSchema

__all__ = ["SEVERITY_ORDER", "Finding", "LintReport", "lint_wiki"]

#: Read top-down by someone with ten minutes. Broken links come first because
#: they are the only class where navigation actually fails; style is last
#: because nothing breaks if it is never addressed.
SEVERITY_ORDER: tuple[str, ...] = ("broken", "structure", "quality", "style")

_SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_ORDER)}

#: A page nobody has touched in this long, whose topic is still being written
#: about, is probably out of date. Ninety days is the hermes figure.
STALE_AFTER = timedelta(days=90)

#: How many over-threshold stubs the report names individually. The rest are
#: summarised in one line — a backlog is a ranked list, not a wall.
STUB_READY_REPORTED = 25

#: Suffixes stripped when deciding whether two titles name the same topic.
#: Longest first, and only one is ever removed. Kept to three because each
#: additional rule buys a rarer pair and risks a commoner false one — English
#: has no short suffix list that gets `policies`→`policy` without also getting
#: `series`→`serie`, and a merge suggestion is cheap to dismiss but expensive
#: to distrust.
_DEDUPE_SUFFIXES = ("ing", "ion", "s")

#: What must survive a strip. Below this, stripping produces fragments that
#: collide with unrelated words — `bus`→`bu`, `ring`→`r`.
_MIN_STEM = 3


def _dedupe_key(title: str) -> str:
    """A normalised form for near-duplicate detection.

    Lowercased, punctuation dropped, and each word reduced past the endings that
    distinguish `checkpoint` from `checkpointing` and `index` from `indexes`.
    Deliberately crude: it is a signal for a human to look, not an automatic
    merge, and a false pair costs one glance while a missed pair costs a topic
    permanently split in two.
    """
    words: list[str] = []
    for raw in re.split(r"[^a-z0-9]+", title.lower()):
        if not raw or raw in {"the", "a", "an", "of", "and", "for", "in", "to"}:
            continue
        stem = raw
        for suffix in _DEDUPE_SUFFIXES:
            if stem.endswith(suffix) and len(stem) - len(suffix) >= _MIN_STEM:
                stem = stem[: -len(suffix)]
                break
        words.append(stem)
    return " ".join(sorted(words))


@dataclass(frozen=True, slots=True)
class Finding:
    """One lint result, addressed to whoever has to fix it."""

    check: str
    severity: str
    message: str
    fix: str = ""
    page_id: UUID | None = None
    page_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "fix": self.fix,
            "page_id": str(self.page_id) if self.page_id else None,
            "page_title": self.page_title,
        }


@dataclass(slots=True)
class LintReport:
    pages_scanned: int = 0
    stubs_skipped: int = 0
    findings: list[Finding] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for f in self.findings:
            counts[f.severity] += 1
        return dict(counts)

    @property
    def by_check(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for f in self.findings:
            counts[f.check] += 1
        return dict(counts)

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (_SEVERITY_RANK.get(f.severity, 99), f.check, f.page_title),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages_scanned": self.pages_scanned,
            "stubs_skipped": self.stubs_skipped,
            "checked_at": self.checked_at.isoformat(),
            "total": len(self.findings),
            "by_severity": self.by_severity,
            "by_check": self.by_check,
            "findings": [f.to_dict() for f in self.sorted_findings()],
        }

    def summary(self) -> str:
        """One-screen text form — what the agent tool returns."""
        if not self.findings:
            return (
                f"Wiki lint — {self.pages_scanned} pages checked "
                f"({self.stubs_skipped} stubs skipped): no findings."
            )
        head = (
            f"Wiki lint — {self.pages_scanned} pages checked "
            f"({self.stubs_skipped} stubs skipped): {len(self.findings)} findings"
        )
        counts = ", ".join(
            f"{n} {sev}"
            for sev, n in sorted(
                self.by_severity.items(), key=lambda kv: _SEVERITY_RANK.get(kv[0], 99)
            )
        )
        lines = [f"{head} — {counts}.", ""]
        current = ""
        for f in self.sorted_findings()[:60]:
            if f.check != current:
                current = f.check
                lines.append(f"[{f.severity}] {f.check}")
            where = f" ({f.page_title})" if f.page_title else ""
            lines.append(f"  - {f.message}{where}" + (f" — {f.fix}" if f.fix else ""))
        if len(self.findings) > 60:
            lines.append(f"  … and {len(self.findings) - 60} more.")
        return "\n".join(lines)


def _current_bodies(project_id: UUID) -> Select[tuple[UUID, str]]:
    """Page id → the body of its current revision.

    Joined on `current_revision_id` rather than taking the max revision number:
    a page's current revision is a pointer, and reading the highest number
    instead would show an unapproved draft as if it were the live page.
    """
    return (
        select(WikiPage.id, WikiRevision.body_md)
        .join(WikiRevision, WikiRevision.id == WikiPage.current_revision_id)
        .where(WikiPage.project_id == project_id)
    )


async def lint_wiki(
    session: AsyncSession,
    *,
    project_id: UUID,
    schema: WikiSchema,
) -> LintReport:
    """Run every check over one project's wiki.

    Stubs are counted and skipped rather than reported: a stub is a title
    something linked to that nobody has written, so every check would fire on
    every stub and the report would be 94% noise — the same failure the review
    queue had before stubs left it.
    """
    report = LintReport()

    pages = list(
        (await session.execute(select(WikiPage).where(WikiPage.project_id == project_id)))
        .scalars()
        .all()
    )
    real = [p for p in pages if not p.is_stub and p.page_type not in EXEMPT_TYPES]
    report.pages_scanned = len(real)
    report.stubs_skipped = len(pages) - len(real)
    if not pages:
        return report

    by_id = {p.id: p for p in pages}
    titles = {p.title.lower() for p in pages}
    slugs = {p.slug for p in pages}

    bodies: dict[UUID, str] = dict(
        (await session.execute(_current_bodies(project_id))).all()  # type: ignore[arg-type]
    )

    # ---- 1. broken wikilinks -------------------------------------------------
    # `dst_page_id IS NULL` is the stored answer, but it is only as fresh as the
    # last link-extraction run, so a title created since would still read as
    # broken. Cross-checking against live titles is what makes the count real.
    broken = (
        await session.execute(
            select(WikiLink.dst_title, WikiLink.src_page_id, func.count())
            .where(WikiLink.project_id == project_id, WikiLink.dst_page_id.is_(None))
            .group_by(WikiLink.dst_title, WikiLink.src_page_id)
        )
    ).all()
    truly_broken: dict[str, set[UUID]] = defaultdict(set)
    for dst_title, src_page_id, _count in broken:
        if str(dst_title).lower() not in titles:
            truly_broken[str(dst_title)].add(src_page_id)
    for dst_title, srcs in truly_broken.items():
        report.findings.append(
            Finding(
                check="broken-wikilink",
                severity="broken",
                message=f"[[{dst_title}]] points at a page that does not exist",
                fix=f"linked from {len(srcs)} page(s) — write it, or fix the link text",
            )
        )

    # ---- 2. orphan pages -----------------------------------------------------
    # No inbound link means no path to the page except search. Hubs are exempt
    # by construction: a hub is what you navigate FROM.
    inbound: set[UUID] = {
        row[0]
        for row in (
            await session.execute(
                select(WikiLink.dst_page_id).where(
                    WikiLink.project_id == project_id, WikiLink.dst_page_id.is_not(None)
                )
            )
        ).all()
        if row[0] is not None
    }
    for page in real:
        if page.page_type == "hub" or page.id in inbound:
            continue
        report.findings.append(
            Finding(
                check="orphan",
                severity="structure",
                message="no other page links here",
                fix="link it from its category hub, or from a page that should mention it",
                page_id=page.id,
                page_title=page.title,
            )
        )

    # ---- 3. uncategorised ----------------------------------------------------
    # Aleph's index is derived from `category`, not hand-maintained, so the
    # hermes "is it in index.md" check becomes "does it have a category". A
    # page without one appears in no hub and no section — it exists and is
    # unreachable by browsing, which is index-incompleteness by another name.
    for page in real:
        if not page.category:
            report.findings.append(
                Finding(
                    check="uncategorised",
                    severity="structure",
                    message="has no category, so it appears under no hub",
                    fix=f"file under one of: {', '.join(sorted(schema.category_ids))}",
                    page_id=page.id,
                    page_title=page.title,
                )
            )
        elif page.category not in schema.category_ids:
            report.findings.append(
                Finding(
                    check="unknown-category",
                    severity="structure",
                    message=f"category {page.category!r} is not in the schema",
                    fix="add the category to the schema, or refile the page",
                    page_id=page.id,
                    page_title=page.title,
                )
            )

    # ---- 4. schema violations (frontmatter, tags, links, length) -------------
    for page in real:
        body = bodies.get(page.id, "")
        fm, _rest = parse(body)
        outbound = len({t.lower() for t in extract_wikilinks(body)})
        body_lines = len(body.splitlines())
        for violation in schema.validate_page(
            title=page.title,
            page_type=page.page_type,
            category=page.category,
            tags=list(page.tags or []),
            related=list(page.related or []),
            confidence=page.confidence,
            contested=page.contested,
            contradictions=list(page.contradictions or []),
            outbound_links=outbound,
            body_lines=body_lines,
            is_stub=page.is_stub,
        ):
            # Category is already reported above with a better message; not
            # reporting it twice is the difference between a report someone
            # reads and one they skim.
            if violation.field == "category":
                continue
            severity = {
                "wikilinks": "structure",
                "length": "style",
                "tags": "structure",
            }.get(violation.field, "quality")
            report.findings.append(
                Finding(
                    check=f"schema:{violation.field}",
                    severity=severity,
                    message=violation.message,
                    fix=violation.fix,
                    page_id=page.id,
                    page_title=page.title,
                )
            )
        # Frontmatter that disagrees with the columns is the drift this whole
        # design exists to prevent: the body says one thing, a query says
        # another, and which is true depends on who asks.
        if fm.title and fm.category and fm.category != page.category:
            report.findings.append(
                Finding(
                    check="frontmatter-drift",
                    severity="broken",
                    message=(
                        f"body frontmatter says category {fm.category!r} but the "
                        f"page is filed under {page.category!r}"
                    ),
                    fix="rewrite the page so the block and the row agree",
                    page_id=page.id,
                    page_title=page.title,
                )
            )

    # ---- 5. contested pages --------------------------------------------------
    for page in real:
        if page.contested:
            named = ", ".join(page.contradictions or []) or "nothing named"
            report.findings.append(
                Finding(
                    check="contested",
                    severity="quality",
                    message=f"marked contested against: {named}",
                    fix="resolve, or record both positions with dates and sources",
                    page_id=page.id,
                    page_title=page.title,
                )
            )

    # ---- 6. quality signals --------------------------------------------------
    # `confidence: null` is not `confidence: high`. A page nobody has judged is
    # reported so weak claims do not silently harden into accepted wiki fact —
    # which is the failure mode the hermes skill names for this check.
    for page in real:
        if page.confidence == "low":
            report.findings.append(
                Finding(
                    check="low-confidence",
                    severity="quality",
                    message="claims are marked low confidence",
                    fix="find corroboration, or narrow the claim to what is supported",
                    page_id=page.id,
                    page_title=page.title,
                )
            )
        elif page.confidence is None:
            report.findings.append(
                Finding(
                    check="unjudged",
                    severity="quality",
                    message="nobody has set a confidence on this page",
                    fix="set high / medium / low — unset is not the same as high",
                    page_id=page.id,
                    page_title=page.title,
                )
            )

    # ---- 7. stale content ----------------------------------------------------
    now = datetime.now(UTC)
    for page in real:
        # `updated_at` is non-nullable, so there is always a date to compare;
        # `verified_at` wins when set because a re-verification is a stronger
        # statement about currency than an incidental edit.
        touched = page.verified_at or page.updated_at
        if touched.tzinfo is None:
            touched = touched.replace(tzinfo=UTC)
        age = now - touched
        if age > STALE_AFTER:
            report.findings.append(
                Finding(
                    check="stale",
                    severity="quality",
                    message=f"not touched in {age.days} days",
                    fix="re-verify against current sources, or mark the volatility cold",
                    page_id=page.id,
                    page_title=page.title,
                )
            )

    # ---- 8. tag audit --------------------------------------------------------
    # Reported once per unknown tag rather than once per page: "`transformerz`
    # is used on 14 pages" is one decision; fourteen findings is fourteen.
    tag_users: dict[str, list[str]] = defaultdict(list)
    for page in real:
        for tag in page.tags or []:
            if tag not in schema.tag_set:
                tag_users[tag].append(page.title)
    for tag, users in tag_users.items():
        report.findings.append(
            Finding(
                check="tag-outside-taxonomy",
                severity="structure",
                message=f"{tag!r} is used on {len(users)} page(s) but is not in the taxonomy",
                fix="add it to the schema, or replace it with a tag that is",
            )
        )

    # ---- 9. duplicate slugs --------------------------------------------------
    # Obsidian resolves `[[slug]]` by shortest path, so two pages sharing a slug
    # makes every link to that slug ambiguous. The database's unique constraint
    # prevents this per project; a collision here means a title normalising onto
    # an existing slug, which is worth naming before it is written.
    if len(slugs) != len(real):
        counts: dict[str, int] = defaultdict(int)
        for page in real:
            counts[page.slug] += 1
        for slug, n in counts.items():
            if n > 1:
                report.findings.append(
                    Finding(
                        check="duplicate-slug",
                        severity="broken",
                        message=f"{n} pages share the slug {slug!r}",
                        fix="every [[slug]] link to it is ambiguous — rename one",
                    )
                )

    # ---- 10. unresolved `related` -------------------------------------------
    for page in real:
        for slug in page.related or []:
            if slug not in slugs:
                report.findings.append(
                    Finding(
                        check="related-missing",
                        severity="structure",
                        message=f"related names {slug!r}, which is not a page here",
                        fix="write it, or drop it from related",
                        page_id=page.id,
                        page_title=page.title,
                    )
                )

    # ---- 11. contradictions pointing nowhere --------------------------------
    for page in real:
        for slug in page.contradictions or []:
            if slug not in slugs:
                report.findings.append(
                    Finding(
                        check="contradiction-missing",
                        severity="broken",
                        message=f"claims to contradict {slug!r}, which does not exist",
                        fix="a contradiction a reader cannot check is worse than none",
                        page_id=page.id,
                        page_title=page.title,
                    )
                )

    # ---- 12. near-duplicate titles ------------------------------------------
    # "Add to existing page rather than creating near-duplicates" is a rule in
    # the schema with nothing enforcing it, and mechanical extraction breaks it
    # constantly: this corpus holds both `checkpoint` and `checkpointing`, which
    # are one topic split across two pages so that neither accumulates the
    # evidence and a reader finds whichever the ranker preferred.
    #
    # Compared on a normalised key rather than by edit distance: cheap, exact,
    # and it catches the cases that actually occur (plural, gerund, hyphen,
    # acronym-with-expansion) without reporting every pair of short titles.
    normalised: dict[str, list[WikiPage]] = defaultdict(list)
    for page in pages:
        normalised[_dedupe_key(page.title)].append(page)
    for key, group in normalised.items():
        if len(group) < 2 or not key:
            continue
        titles_ = ", ".join(sorted(f"{p.title!r}" for p in group))
        report.findings.append(
            Finding(
                check="near-duplicate",
                severity="structure",
                message=f"{len(group)} pages are the same topic: {titles_}",
                fix="merge into one page; split evidence means neither is complete",
            )
        )

    # ---- 13. stubs ready for promotion --------------------------------------
    # Not a defect — the opposite. These have met the threshold and are waiting
    # to become real pages, and surfacing them is what turns the threshold from
    # a rule into a work queue.
    ready = (
        await session.execute(
            select(WikiLink.dst_page_id, func.count(func.distinct(WikiLink.src_page_id)))
            .where(WikiLink.project_id == project_id, WikiLink.dst_page_id.is_not(None))
            .group_by(WikiLink.dst_page_id)
            .having(
                func.count(func.distinct(WikiLink.src_page_id)) >= schema.stub_promotion_mentions
            )
        )
    ).all()
    # Ranked, and capped. On a machine-extracted corpus this list runs to
    # hundreds; reporting all of them turns the writing backlog into the same
    # undifferentiated wall the review queue used to be. The top of the list by
    # citation count is the part that is actually actionable.
    earned = sorted(
        (
            (by_id[page_id], mentions)
            for page_id, mentions in ready
            if page_id in by_id
            and by_id[page_id].is_stub
            and by_id[page_id].status != WRITING_QUEUE
        ),
        key=lambda pair: -pair[1],
    )
    for page, mentions in earned[:STUB_READY_REPORTED]:
        report.findings.append(
            Finding(
                check="stub-ready",
                severity="quality",
                message=f"{mentions} pages cite this unwritten title — it has earned a page",
                fix="write it; promotion moves it to the writing queue, not to review",
                page_id=page.id,
                page_title=page.title,
            )
        )
    if len(earned) > STUB_READY_REPORTED:
        report.findings.append(
            Finding(
                check="stub-ready",
                severity="quality",
                message=(
                    f"{len(earned) - STUB_READY_REPORTED} more unwritten titles are over "
                    f"the threshold of {schema.stub_promotion_mentions}"
                ),
                fix="raise the threshold if this backlog is not real work",
            )
        )

    return report
