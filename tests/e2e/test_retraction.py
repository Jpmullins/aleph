"""Integration: source retraction + blast-radius (WP-6 §4 / F4 item 3).

Retracting a source cited by TWO pages must:
  - set `Source.status="retracted"` + `retracted_at` and write a `source.retract`
    ledger event;
  - flag both pages' dependent claims (`confidence="retracted"`,
    `status="contested"`) with a `wiki_claim.retract_flag` ledger event each;
  - be queryable via `dependent_claims`;
  - emit a critical `retracted_source` ReviewFinding into Briefs.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-retract", "email": "retract@test.local", "name": "Retract"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_retract_source_flags_dependent_claims_on_two_pages(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_core.ids import uuid7
    from aleph_core.time import utcnow
    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_reviewer.models import ReviewFinding
    from aleph_reviewer.retraction import dependent_claims, retract_source
    from aleph_rks.models import Source, SourceVersion
    from aleph_security.principal import Principal
    from aleph_wiki.models import SourcePage, WikiClaim, WikiPage
    from aleph_wiki.wiki_service import CitationDraft, ClaimDraft, WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post("/v1/projects", json={"title": "Retract", "description": "x"})
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    # --- seed: 1 source (its SourcePage bridge) cited by claims on TWO pages ---
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)

        # The source's own representation page (SourcePage.page_id).
        srcpage = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Source Doc",
            slug=None,
            page_kind="source",
            body_md="# Source Doc\n\nraw\n",
            summary="src",
            claims=[],
            wikilinks=[],
            commit_message="seed source page",
        )

        source = Source(
            id=uuid7(),
            project_id=pid,
            connector_kind="upload",
            title="Retract Me",
            short_id=uuid7().hex[:16],
            status="ingested",
            created_by=owner,
            access_scope="project",
        )
        session.add(source)
        await session.flush()
        session.add(
            SourceVersion(
                id=uuid7(),
                source_id=source.id,
                version_no=1,
                asset_id=uuid7(),
                sha256="0" * 64,
                fetched_at=utcnow(),
                created_by=owner,
                access_scope="project",
            )
        )
        bridge = SourcePage(
            id=uuid7(),
            project_id=pid,
            source_id=source.id,
            page_id=srcpage.page_id,
            extracted_claims_jsonb=[],
            extracted_at=utcnow(),
        )
        session.add(bridge)
        await session.flush()

        # source_id is what retraction walks. Seeding only source_page_id — the
        # column no writer populates — is how this test passed while the
        # feature returned nothing.
        cite = CitationDraft(
            chunk_ids=[],
            source_id=source.id,
            source_page_id=bridge.id,
            citation_marker="[1]",
        )
        a = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Page A",
            slug=None,
            page_kind="topic",
            body_md="# Page A\n\nClaim A [1].\n",
            summary="a",
            claims=[
                ClaimDraft(text="A relies on the source.", confidence="cited", citations=[cite])
            ],
            wikilinks=[],
            commit_message="seed A",
        )
        b = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Page B",
            slug=None,
            page_kind="topic",
            body_md="# Page B\n\nClaim B [1].\n",
            summary="b",
            claims=[
                ClaimDraft(text="B relies on the source.", confidence="cited", citations=[cite])
            ],
            wikilinks=[],
            commit_message="seed B",
        )
        await session.commit()
        source_id = source.id
        a_page, b_page = a.page_id, b.page_id

    # --- blast-radius is queryable BEFORE retraction ---
    async with maker() as session:
        deps = await dependent_claims(session, source_id)
        assert {p for p, _ in deps} == {a_page, b_page}

    # --- retract ---
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="user", email="", actor_kind="user")
        result = await retract_source(
            session,
            LedgerWriter(session),
            principal,
            source_id=source_id,
            reason="retracted upstream",
        )
        await session.commit()
        assert result.page_ids == {a_page, b_page}
        assert len(result.claim_ids) == 2
        assert result.finding_id is not None

    # --- verify state ---
    async with maker() as session:
        src = (await session.execute(select(Source).where(Source.id == source_id))).scalar_one()
        assert src.status == "retracted"
        assert src.retracted_at is not None
        assert src.retraction_reason == "retracted upstream"

        for page_id in (a_page, b_page):
            page = (
                await session.execute(select(WikiPage).where(WikiPage.id == page_id))
            ).scalar_one()
            claims = list(
                (
                    await session.execute(
                        select(WikiClaim).where(WikiClaim.revision_id == page.current_revision_id)
                    )
                )
                .scalars()
                .all()
            )
            assert claims, f"page {page_id} has no claims"
            # `status` carries the retraction; `confidence` stays derived from
            # the evidence and has no "retracted" state for a recompute to write.
            assert all(c.status == "retracted" for c in claims)

        retract_events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == pid,
                        ActionLedgerEvent.action_kind == "source.retract",
                        ActionLedgerEvent.target_id == source_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(retract_events) == 1

        flag_events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == pid,
                        ActionLedgerEvent.action_kind == "wiki_claim.retract_flag",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(flag_events) == 2

        findings = list(
            (
                await session.execute(
                    select(ReviewFinding).where(
                        ReviewFinding.project_id == pid,
                        ReviewFinding.finding_kind == "retracted_source",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert findings, "no retracted_source finding emitted"
        assert any(f.target_source_id == source_id and f.severity == "critical" for f in findings)

    # --- idempotent: second retract is a no-op that still reports the blast-radius ---
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="user", email="", actor_kind="user")
        again = await retract_source(
            session,
            LedgerWriter(session),
            principal,
            source_id=source_id,
            reason="again",
        )
        await session.commit()
        assert again.already_retracted is True
        assert again.page_ids == {a_page, b_page}
