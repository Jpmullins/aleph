"""apply_merge folds the source page's claims into the target (provenance-preserving merge)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-fold", "email": "fold@test.local", "name": "Fold"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_apply_merge_folds_source_claims_into_target(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import PageMergeProposal, WikiClaim, WikiPage
    from aleph_wiki.wiki_service import ClaimDraft, WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post("/v1/projects", json={"title": "Fold", "description": "x"})
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)
        src = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Dup Source",
            slug=None,
            page_kind="topic",
            body_md="# Dup Source\n\nx\n",
            summary="src",
            claims=[ClaimDraft(text="Source-only claim about X.", confidence="cited")],
            wikilinks=[],
            commit_message="seed source",
        )
        tgt = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Dup Target",
            slug=None,
            page_kind="topic",
            body_md="# Dup Target\n\ny\n",
            summary="tgt",
            claims=[ClaimDraft(text="Target claim about Y.", confidence="cited")],
            wikilinks=[],
            commit_message="seed target",
        )
        prop = PageMergeProposal(
            id=uuid7(),
            project_id=pid,
            source_page_id=src.page_id,
            target_page_id=tgt.page_id,
            rationale="dup",
            similarity=0.9,
            status="pending",
            created_by=owner,
        )
        session.add(prop)
        await session.commit()
        prop_id, tgt_id = prop.id, tgt.page_id

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="agent", email="", actor_kind="aleph_agent")
        prop = (
            await session.execute(select(PageMergeProposal).where(PageMergeProposal.id == prop_id))
        ).scalar_one()
        await CuratorService(session, ledger=LedgerWriter(session)).apply_merge(
            proposal=prop, principal=principal, ledger=LedgerWriter(session)
        )
        await session.commit()

    # The target's current revision now carries BOTH its own claim and the
    # source's claim — the merge preserved the source's provenance.
    async with maker() as session:
        tgt = (await session.execute(select(WikiPage).where(WikiPage.id == tgt_id))).scalar_one()
        claim_texts = {
            c.text
            for c in (
                await session.execute(
                    select(WikiClaim).where(WikiClaim.revision_id == tgt.current_revision_id)
                )
            )
            .scalars()
            .all()
        }
        assert "Target claim about Y." in claim_texts
        assert "Source-only claim about X." in claim_texts, "merge dropped the source's claims"
