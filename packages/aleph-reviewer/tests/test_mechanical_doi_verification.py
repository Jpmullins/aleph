"""Integration tests for the MechanicalReviewer `doi_verification` node (WP-2 §6).

Requires Postgres (compose stack) at DATABASE_URL with migrations applied.
The scholar service is stubbed — no network calls; verdicts are injected
through the workflow's `scholar` constructor param.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aleph_core.ids import uuid7
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_reviewer.mechanical import MechanicalReviewerWorkflow
from aleph_reviewer.models import ReviewFinding
from aleph_rks.models import Source
from aleph_scholar import DoiVerdict, ScholarUpstreamError
from aleph_security.principal import Principal
from aleph_wiki.belief_service import claim_key_for
from aleph_wiki.models import Citation, SourcePage, WikiClaim, WikiRevision

pytestmark = pytest.mark.integration

_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://aleph:changeme-ci@localhost:5432/aleph",
)

FABRICATED_DOI = "10.9999/aleph-fabricated-doi"
RETRACTED_DOI = "10.1016/s0140-6736(97)11096-0"
UNVERIFIABLE_DOI = "10.5555/aleph-unverifiable-doi"


def _verdict(
    doi: str,
    *,
    ok: bool | None,
    retracted: bool | None = None,
    checked_via: str = "crossref+openalex",
) -> DoiVerdict:
    return DoiVerdict(
        doi=doi,
        ok=ok,
        retracted=retracted,
        title=None,
        year=None,
        openalex_id=None,
        checked_via=checked_via,
    )


class StubScholar:
    """Injectable stand-in for ScholarService.verify_dois — no network."""

    def __init__(
        self,
        verdicts: dict[str, DoiVerdict] | None = None,
        *,
        raise_upstream: bool = False,
    ) -> None:
        self._verdicts = verdicts or {}
        self._raise_upstream = raise_upstream
        self.calls: list[list[str]] = []

    async def verify_dois(self, dois: list[str]) -> list[DoiVerdict]:
        self.calls.append(list(dois))
        if self._raise_upstream:
            raise ScholarUpstreamError("scholar upstream down")
        return [self._verdicts.get(d, _verdict(d, ok=True, retracted=False)) for d in dois]


@pytest.fixture
async def session_maker():
    engine = create_async_engine(_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_revision(
    session: AsyncSession,
    *,
    project_id: UUID,
    body_md: str,
    source_doi: str | None = None,
    source_metadata_extra: dict[str, object] | None = None,
) -> tuple[UUID, UUID, UUID | None]:
    """Seed page/revision (+ optional registry source carrying `source_doi`).

    Returns (page_id, revision_id, source_id).
    """
    user_id = uuid4()
    page_id = uuid4()
    revision = WikiRevision(
        id=uuid7(),
        page_id=page_id,
        project_id=project_id,
        revision_no=1,
        body_md=body_md,
        author_kind="agent",
        author_id=user_id,
        body_sha256=hashlib.sha256(body_md.encode()).hexdigest(),
        ledger_event_id=uuid7(),
    )
    session.add(revision)

    source_id: UUID | None = None
    if source_doi is not None:
        source = Source(
            id=uuid7(),
            project_id=project_id,
            connector_kind="upload",
            title="Seeded scholarly source",
            source_metadata_jsonb={"doi": source_doi, **(source_metadata_extra or {})},
            short_id=uuid4().hex[:12],
            created_by=user_id,
        )
        session.add(source)
        source_id = source.id
        source_page = SourcePage(
            id=uuid7(),
            project_id=project_id,
            source_id=source.id,
            page_id=uuid4(),
            extracted_at=datetime.now(UTC),
        )
        session.add(source_page)
        claim_text = "A claim backed by the seeded source."
        claim = WikiClaim(
            id=uuid7(),
            project_id=project_id,
            page_id=page_id,
            revision_id=revision.id,
            text=claim_text,
            # Keyed, like a row the production write path would produce.
            #
            # `BeliefService.upsert_claim` derives `claim_key` from the
            # proposition on every write, and it is the column claim IDENTITY
            # rests on (WS-RS8 c3). Leaving it NULL here made the fixture
            # describe a row the pipeline cannot emit — and, because this test
            # commits against the live database and does not clean up, every
            # run added two rows to the very health count that asks "how many
            # claims have no identity". Measured while auditing the rebuilt
            # image: the only NULL-key claims written after the two stale
            # writers were gone came from HERE, two per run, in throwaway
            # uuid4 projects.
            claim_key=claim_key_for(claim_text),
            created_by=user_id,
        )
        session.add(claim)
        session.add(
            Citation(
                id=uuid7(),
                project_id=project_id,
                claim_id=claim.id,
                # source_id is the anchor retraction walks; source_page_id was
                # never populated by any writer, so seeding only it made this
                # fixture describe a row the pipeline could not produce.
                source_id=source_id,
                source_page_id=source_page.id,
                citation_marker="1",
            )
        )
    await session.commit()
    return page_id, revision.id, source_id


def _workflow(session_maker, scholar: StubScholar) -> MechanicalReviewerWorkflow:
    principal = Principal(user_id=uuid4(), subject="agent", email="", actor_kind="aleph_agent")
    return MechanicalReviewerWorkflow(
        session_maker=session_maker, principal=principal, scholar=scholar
    )


async def _findings(session: AsyncSession, project_id: UUID) -> list[ReviewFinding]:
    return list(
        (await session.execute(select(ReviewFinding).where(ReviewFinding.project_id == project_id)))
        .scalars()
        .all()
    )


async def _source_update_count(session: AsyncSession, project_id: UUID) -> int:
    return (
        await session.execute(
            select(func.count(ActionLedgerEvent.id)).where(
                ActionLedgerEvent.project_id == project_id,
                ActionLedgerEvent.action_kind == "source.update",
            )
        )
    ).scalar_one()


async def test_fabricated_and_retracted_dois_yield_exact_findings(session_maker) -> None:
    project_id = uuid4()
    body = (
        "Fabricated claim (doi:10.9999/aleph-fabricated-doi) and a retracted one "
        "(https://doi.org/10.1016/S0140-6736(97)11096-0)."
    )
    async with session_maker() as session:
        page_id, revision_id, source_id = await _seed_revision(
            session, project_id=project_id, body_md=body, source_doi=RETRACTED_DOI
        )
        ledger_before = await _source_update_count(session, project_id)

    scholar = StubScholar(
        {
            FABRICATED_DOI: _verdict(FABRICATED_DOI, ok=False),
            RETRACTED_DOI: _verdict(RETRACTED_DOI, ok=True, retracted=True),
        }
    )
    n = await _workflow(session_maker, scholar).run(
        project_id=project_id,
        revision_id=revision_id,
        page_id=page_id,
        agent_run_id=uuid7(),
    )
    assert n == 2
    assert scholar.calls == [[FABRICATED_DOI, RETRACTED_DOI]]

    async with session_maker() as session:
        findings = await _findings(session, project_id)
        assert {(f.finding_kind, f.severity) for f in findings} == {
            ("fabricated_doi", "high"),
            ("retracted_source", "critical"),
        }
        by_kind = {f.finding_kind: f for f in findings}

        fabricated = by_kind["fabricated_doi"]
        assert FABRICATED_DOI in fabricated.title
        assert fabricated.target_page_id == page_id
        assert fabricated.target_revision_id == revision_id
        assert fabricated.target_source_id is None  # body-only DOI
        assert fabricated.evidence_refs_jsonb == [
            {"kind": "doi", "value": FABRICATED_DOI},
            {"kind": "checked_via", "value": "crossref+openalex"},
        ]

        # Scholar-detected retraction now funnels through the shared
        # `retract_source` service (WP-6 §4): the finding names the retracted
        # source (not the DOI), the source is retracted, and its dependent claim
        # is flagged. It targets the source, not the page/revision.
        retracted = by_kind["retracted_source"]
        source = await session.get(Source, source_id)
        assert source is not None
        assert source.short_id in retracted.title
        assert retracted.target_source_id == source_id  # DOI came from the source row
        assert retracted.target_page_id is None
        assert retracted.target_revision_id is None
        # The source, then one ref per claim the retraction reached. WS-RS9
        # added the claim refs: a critical finding that names only the source
        # leaves the blast radius recoverable solely by re-running the query,
        # which is the same as not reporting it.
        assert retracted.evidence_refs_jsonb[0] == {
            "kind": "source",
            "source_id": str(source_id),
            "short_id": source.short_id,
        }
        claim_refs = retracted.evidence_refs_jsonb[1:]
        assert claim_refs, "the finding names no claim, so it does not say what it reached"
        assert {ref["kind"] for ref in claim_refs} == {"wiki_claim"}
        assert {ref["hop"] for ref in claim_refs} == {"direct"}
        assert source.status == "retracted"
        assert source.retracted_at is not None

        # The claim backed by the retracted source is flagged retracted/contested.
        flagged = list(
            (await session.execute(select(WikiClaim).where(WikiClaim.revision_id == revision_id)))
            .scalars()
            .all()
        )
        assert flagged
        # `status` carries the retraction. `confidence` is derived from evidence
        # and has no "retracted" state, so writing it there would be erased by
        # the next recompute.
        assert all(c.status == "retracted" for c in flagged)

        # Verdict cached back onto the source row, in the findings transaction.
        cached = source.source_metadata_jsonb["doi_verdict"]
        assert cached["ok"] is True
        assert cached["retracted"] is True
        assert cached["checked_via"] == "crossref+openalex"
        assert "checked_at" in cached

        # Exactly one source.update ledger event for the cached verdict.
        assert await _source_update_count(session, project_id) == ledger_before + 1


async def test_unverifiable_doi_yields_no_finding(session_maker) -> None:
    project_id = uuid4()
    body = f"An unverifiable citation: https://doi.org/{UNVERIFIABLE_DOI}"
    async with session_maker() as session:
        page_id, revision_id, source_id = await _seed_revision(
            session,
            project_id=project_id,
            body_md=body,
            source_doi=UNVERIFIABLE_DOI,
            source_metadata_extra={"note": "pre-existing"},
        )

    scholar = StubScholar({UNVERIFIABLE_DOI: _verdict(UNVERIFIABLE_DOI, ok=None)})
    n = await _workflow(session_maker, scholar).run(
        project_id=project_id,
        revision_id=revision_id,
        page_id=page_id,
        agent_run_id=uuid7(),
    )
    assert n == 0
    assert scholar.calls == [[UNVERIFIABLE_DOI]]

    async with session_maker() as session:
        assert await _findings(session, project_id) == []
        # ok=None is never cached and never ledgered.
        source = await session.get(Source, source_id)
        assert source is not None
        assert "doi_verdict" not in source.source_metadata_jsonb
        assert source.source_metadata_jsonb["note"] == "pre-existing"
        assert await _source_update_count(session, project_id) == 0


async def test_upstream_error_never_fails_the_run(session_maker) -> None:
    project_id = uuid4()
    body = f"Cites {FABRICATED_DOI} while the scholar upstream is down."
    async with session_maker() as session:
        page_id, revision_id, _ = await _seed_revision(session, project_id=project_id, body_md=body)

    scholar = StubScholar(raise_upstream=True)
    n = await _workflow(session_maker, scholar).run(
        project_id=project_id,
        revision_id=revision_id,
        page_id=page_id,
        agent_run_id=uuid7(),
    )
    assert n == 0
    assert scholar.calls == [[FABRICATED_DOI]]
    async with session_maker() as session:
        assert await _findings(session, project_id) == []


async def test_cap_limits_verification_to_fifty_dois(session_maker) -> None:
    project_id = uuid4()
    dois = [f"10.1234/aleph-cap-{i:03d}" for i in range(60)]
    body = "\n".join(f"- https://doi.org/{d}" for d in dois)
    async with session_maker() as session:
        page_id, revision_id, _ = await _seed_revision(session, project_id=project_id, body_md=body)

    scholar = StubScholar({dois[0]: _verdict(dois[0], ok=False)})
    n = await _workflow(session_maker, scholar).run(
        project_id=project_id,
        revision_id=revision_id,
        page_id=page_id,
        agent_run_id=uuid7(),
    )
    assert scholar.calls == [dois[:50]]  # tail skipped, single batched call
    assert n == 1
    async with session_maker() as session:
        findings = await _findings(session, project_id)
        assert [f.finding_kind for f in findings] == ["fabricated_doi"]
        # The skipped tail is noted in the finding description.
        assert "10 additional DOI(s)" in findings[0].description
