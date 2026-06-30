"""apply_merge rewrites [[Source]] -> [[Target]] in inbound page bodies (audit F23)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-merge", "email": "merge@test.local", "name": "Merge"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_apply_merge_rewrites_inbound_bodies(http_client, auth_bypass, asgi_app, monkeypatch):
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import PageMergeProposal, WikiPage, WikiRevision
    from aleph_wiki.wiki_service import WikiLinkDraft, WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post(
        "/v1/projects", json={"title": "Merge Body", "description": "x", "budget_usd": "1.00"}
    )
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)
        source = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Source Topic",
            slug=None,
            page_kind="topic",
            body_md="# Source Topic\n\nThe source.\n",
            summary="Source Topic",
            claims=[],
            wikilinks=[],
            commit_message="seed",
        )
        await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Target Topic",
            slug=None,
            page_kind="topic",
            body_md="# Target Topic\n\nThe target.\n",
            summary="Target Topic",
            claims=[],
            wikilinks=[],
            commit_message="seed",
        )
        # Citing Page links Source in prose AND has a resolved WikiLink row.
        await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Citing Page",
            slug=None,
            page_kind="topic",
            body_md="# Citing Page\n\nThis cites [[Source Topic]] in prose.\n",
            summary="Citing Page",
            claims=[],
            wikilinks=[
                WikiLinkDraft(dst_title="Source Topic", dst_page_id=source.page_id, occurrences=1)
            ],
            commit_message="seed",
        )
        await session.commit()

    # Resolve ids + build the merge proposal (source -> target).
    async with maker() as session:
        pages = {
            p.title: p
            for p in (
                await session.execute(select(WikiPage).where(WikiPage.project_id == pid))
            ).scalars()
        }
        from aleph_core.ids import uuid7

        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        prop = PageMergeProposal(
            id=uuid7(),
            project_id=pid,
            source_page_id=pages["Source Topic"].id,
            target_page_id=pages["Target Topic"].id,
            rationale="dup",
            similarity=0.9,
            status="pending",
            created_by=owner,
        )
        session.add(prop)
        await session.commit()
        prop_id, citing_id = prop.id, pages["Citing Page"].id

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

    # The Citing Page body now references the target, not the source.
    async with maker() as session:
        citing = (
            await session.execute(select(WikiPage).where(WikiPage.id == citing_id))
        ).scalar_one()
        rev = (
            await session.execute(
                select(WikiRevision).where(WikiRevision.id == citing.current_revision_id)
            )
        ).scalar_one()
        assert "[[Target Topic]]" in rev.body_md
        assert "[[Source Topic]]" not in rev.body_md
