"""The wiki lint, against a real corpus it builds and then tears down.

Each test constructs the exact defect it is about, asserts the lint names it,
and — the part that matters — asserts the lint does NOT name it once fixed.
A checker that only ever fires is indistinguishable from one that is hardcoded
to fire, which is the failure `scripts/acceptance.sh --self-check` exists to
prevent for the sweeps.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_wiki.lint import lint_wiki
from aleph_wiki.models import WikiLink, WikiPage
from aleph_wiki.schema import WRITING_QUEUE, default_schema

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _page(
    project_id: uuid.UUID,
    title: str,
    *,
    slug: str | None = None,
    category: str | None = "architectures",
    page_type: str | None = "concept",
    tags: list[str] | None = None,
    confidence: str | None = "high",
    is_stub: bool = False,
    status: str = "draft",
    contested: bool = False,
    contradictions: list[str] | None = None,
    related: list[str] | None = None,
) -> WikiPage:
    return WikiPage(
        id=uuid.uuid4(),
        project_id=project_id,
        title=title,
        slug=slug or title.lower().replace(" ", "-"),
        page_kind="stub" if is_stub else "topic",
        is_stub=is_stub,
        status=status,
        category=category,
        page_type=page_type,
        tags=tags if tags is not None else ["architecture", "method"],
        related=related or [],
        confidence=confidence,
        contested=contested,
        contradictions=contradictions or [],
        created_by=ACTOR,
    )


@pytest.fixture
async def project(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    pid = uuid.uuid4()
    yield pid
    # Roll back rather than delete: nothing here was committed, so the rows
    # vanish with the transaction and no cleanup can miss a table.
    await session.rollback()


async def _lint(session: AsyncSession, project_id: uuid.UUID, **overrides: object):
    schema = default_schema()
    for key, value in overrides.items():
        setattr(schema, key, value)
    return await lint_wiki(session, project_id=project_id, schema=schema)


def _checks(report: object) -> set[str]:
    return {f.check for f in report.findings}  # type: ignore[attr-defined]


class TestBrokenLinks:
    async def test_a_link_to_nothing_is_reported(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        src = _page(project, "Source Page")
        session.add(src)
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=src.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=None,
                dst_title="Never Written",
                occurrences=1,
            )
        )
        await session.flush()

        report = await _lint(session, project)
        assert "broken-wikilink" in _checks(report)
        assert any("Never Written" in f.message for f in report.findings)

    async def test_a_link_whose_target_now_exists_is_not_reported(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """`dst_page_id` is only as fresh as the last extraction run.

        Without the live cross-check, writing the missing page leaves the link
        reading as broken until something re-extracts — so the report would
        keep naming a problem the user already fixed.
        """
        src = _page(project, "Source Page")
        target = _page(project, "Later Written")
        session.add_all([src, target])
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=src.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=None,  # stale: extraction has not re-run
                dst_title="Later Written",
                occurrences=1,
            )
        )
        await session.flush()

        report = await _lint(session, project)
        assert not any("Later Written" in f.message for f in report.findings)


class TestOrphans:
    async def test_a_page_nothing_links_to_is_an_orphan(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        session.add(_page(project, "Unreachable"))
        await session.flush()
        report = await _lint(session, project)
        assert "orphan" in _checks(report)

    async def test_a_linked_page_is_not_an_orphan(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        src, dst = _page(project, "Src"), _page(project, "Dst")
        session.add_all([src, dst])
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=src.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=dst.id,
                dst_title="Dst",
                occurrences=1,
            )
        )
        await session.flush()
        report = await _lint(session, project)
        assert not any(f.page_title == "Dst" and f.check == "orphan" for f in report.findings)

    async def test_a_hub_is_never_an_orphan(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """A hub is what you navigate FROM. Requiring inbound links to it
        inverts the structure it exists to provide."""
        session.add(_page(project, "Architectures Hub", page_type="hub"))
        await session.flush()
        report = await _lint(session, project)
        assert not any(f.check == "orphan" for f in report.findings)


class TestSchemaChecks:
    async def test_a_tag_outside_the_taxonomy_is_reported_once_per_tag(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """Once per tag, not once per page: "used on 14 pages" is one
        decision; fourteen findings is fourteen."""
        for i in range(3):
            session.add(_page(project, f"Page {i}", slug=f"page-{i}", tags=["bogustag"]))
        await session.flush()
        report = await _lint(session, project)
        tag_findings = [f for f in report.findings if f.check == "tag-outside-taxonomy"]
        assert len(tag_findings) == 1
        assert "3 page(s)" in tag_findings[0].message

    async def test_an_uncategorised_page_is_reported(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        session.add(_page(project, "Homeless", category=None))
        await session.flush()
        assert "uncategorised" in _checks(await _lint(session, project))

    async def test_a_categorised_page_is_not(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        session.add(_page(project, "Filed", category="architectures"))
        await session.flush()
        assert "uncategorised" not in _checks(await _lint(session, project))

    async def test_related_naming_a_missing_page_is_reported(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        session.add(_page(project, "Has Related", related=["does-not-exist"]))
        await session.flush()
        assert "related-missing" in _checks(await _lint(session, project))

    async def test_a_contradiction_pointing_nowhere_is_broken_severity(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """A contradiction a reader cannot check is worse than none."""
        session.add(_page(project, "Disputed", contested=True, contradictions=["ghost-page"]))
        await session.flush()
        report = await _lint(session, project)
        missing = [f for f in report.findings if f.check == "contradiction-missing"]
        assert len(missing) == 1
        assert missing[0].severity == "broken"


class TestQualitySignals:
    async def test_an_unjudged_page_is_reported(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """`confidence: null` is not `confidence: high`, and treating it as
        high is how weak claims harden into accepted wiki fact."""
        session.add(_page(project, "Unjudged", confidence=None))
        await session.flush()
        assert "unjudged" in _checks(await _lint(session, project))

    async def test_a_judged_page_is_not(self, session: AsyncSession, project: uuid.UUID) -> None:
        session.add(_page(project, "Judged", confidence="high"))
        await session.flush()
        assert "unjudged" not in _checks(await _lint(session, project))

    async def test_low_confidence_is_reported_separately(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        session.add(_page(project, "Weak", confidence="low"))
        await session.flush()
        checks = _checks(await _lint(session, project))
        assert "low-confidence" in checks
        assert "unjudged" not in checks


class TestStubs:
    async def test_stubs_are_skipped_not_reported(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """Every check would fire on every stub, and the report would be the
        same undifferentiated wall the review queue used to be."""
        for i in range(5):
            session.add(
                _page(
                    project,
                    f"Stub {i}",
                    slug=f"stub-{i}",
                    is_stub=True,
                    status="stub",
                    category=None,
                    page_type=None,
                    tags=[],
                    confidence=None,
                )
            )
        await session.flush()
        report = await _lint(session, project)
        assert report.stubs_skipped == 5
        assert report.pages_scanned == 0
        assert not any(f.check in {"uncategorised", "unjudged"} for f in report.findings)

    async def test_an_over_threshold_stub_is_surfaced_as_work(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        stub = _page(project, "Wanted", is_stub=True, status="stub", category=None, page_type=None)
        sources = [_page(project, f"Src {i}", slug=f"src-{i}") for i in range(3)]
        session.add_all([stub, *sources])
        await session.flush()
        for src in sources:
            session.add(
                WikiLink(
                    id=uuid.uuid4(),
                    project_id=project,
                    src_page_id=src.id,
                    src_revision_id=uuid.uuid4(),
                    dst_page_id=stub.id,
                    dst_title="Wanted",
                    occurrences=1,
                )
            )
        await session.flush()

        over = await _lint(session, project, stub_promotion_mentions=3)
        assert any(f.check == "stub-ready" and f.page_title == "Wanted" for f in over.findings)

        under = await _lint(session, project, stub_promotion_mentions=4)
        assert not any(f.check == "stub-ready" for f in under.findings)

    async def test_a_stub_already_in_the_writing_queue_is_not_re_reported(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """Otherwise the backlog re-announces every item on every run."""
        stub = _page(
            project,
            "Queued",
            is_stub=True,
            status=WRITING_QUEUE,
            category=None,
            page_type=None,
        )
        src = _page(project, "Src")
        session.add_all([stub, src])
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=src.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=stub.id,
                dst_title="Queued",
                occurrences=1,
            )
        )
        await session.flush()
        report = await _lint(session, project, stub_promotion_mentions=1)
        assert not any(f.check == "stub-ready" for f in report.findings)


class TestReportShape:
    async def test_an_empty_project_reports_nothing(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        report = await _lint(session, project)
        assert report.findings == []
        assert "no findings" in report.summary()

    async def test_severity_ordering_puts_broken_first(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """The report is read top-down by someone with ten minutes."""
        src = _page(project, "Src", confidence=None)
        session.add(src)
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=src.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=None,
                dst_title="Missing",
                occurrences=1,
            )
        )
        await session.flush()
        ordered = (await _lint(session, project)).sorted_findings()
        assert ordered[0].severity == "broken"
        assert ordered[-1].severity in {"quality", "style"}


class TestNearDuplicates:
    async def test_a_gerund_and_its_stem_are_one_topic(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """The real corpus holds both `Checkpoint` and `Checkpointing` — one
        topic across two pages, so neither accumulates the evidence."""
        session.add_all([_page(project, "Checkpoint"), _page(project, "Checkpointing")])
        await session.flush()
        report = await _lint(session, project)
        dupes = [f for f in report.findings if f.check == "near-duplicate"]
        assert len(dupes) == 1
        assert "Checkpoint" in dupes[0].message and "Checkpointing" in dupes[0].message

    async def test_plurals_and_hyphens_normalise_together(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        session.add_all(
            [
                _page(project, "Write-Ahead Log", slug="write-ahead-log"),
                _page(project, "write ahead logs", slug="write-ahead-logs"),
            ]
        )
        await session.flush()
        assert "near-duplicate" in _checks(await _lint(session, project))

    async def test_genuinely_different_titles_are_not_paired(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """A false pair costs one glance, but a checker that pairs everything
        is one nobody reads."""
        session.add_all(
            [
                _page(project, "Erasure Coding", slug="erasure-coding"),
                _page(project, "Garbage Collection", slug="garbage-collection"),
                _page(project, "Write Amplification", slug="write-amplification"),
            ]
        )
        await session.flush()
        assert "near-duplicate" not in _checks(await _lint(session, project))


class TestLintAgreesWithRepair:
    """What the lint calls broken must be what the repair cannot fix.

    They resolved links by different rules: the repair matched title, then
    case-insensitive title, then slug; the lint matched title only. So
    `[[Source:S0002]]` read as broken forever while the repair resolved it by
    slug on every run — a report naming a problem that repairing could not
    remove, and a count that never went down.
    """

    async def test_a_slug_form_link_is_not_reported_as_broken(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        target = _page(project, "Source: How to Write to SSDs", slug="source-s0002")
        src = _page(project, "Citing Page", slug="citing-page")
        session.add_all([target, src])
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=src.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=None,
                dst_title="Source:S0002",  # the slug form the compiler emits
                occurrences=1,
            )
        )
        await session.flush()
        report = await _lint(session, project)
        assert not any(f.check == "broken-wikilink" for f in report.findings)

    async def test_a_self_link_is_not_reported_as_broken(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """The repair refuses to point a link at its own page, so reporting one
        as broken names something no repair will ever fix."""
        page = _page(project, "Checkpointing", slug="checkpointing")
        session.add(page)
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=page.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=None,
                dst_title="Checkpointing",
                occurrences=1,
            )
        )
        await session.flush()
        report = await _lint(session, project)
        assert not any(f.check == "broken-wikilink" for f in report.findings)

    async def test_a_link_to_nothing_is_still_broken(
        self, session: AsyncSession, project: uuid.UUID
    ) -> None:
        """Guard the guard: the two exemptions must not swallow real breakage."""
        src = _page(project, "Citing Page", slug="citing-page")
        session.add(src)
        await session.flush()
        session.add(
            WikiLink(
                id=uuid.uuid4(),
                project_id=project,
                src_page_id=src.id,
                src_revision_id=uuid.uuid4(),
                dst_page_id=None,
                dst_title="Genuinely Absent",
                occurrences=1,
            )
        )
        await session.flush()
        report = await _lint(session, project)
        assert any(f.check == "broken-wikilink" for f in report.findings)
