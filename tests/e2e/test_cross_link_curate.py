"""Curator cross_link: a page that mentions a sibling in prose gets linked (audit F03)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-xlink", "email": "xlink@test.local", "name": "XLink"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_curate_cross_links_sibling_mentioned_in_prose(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import WikiLink
    from aleph_wiki.wiki_service import WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)

    proj = await http_client.post(
        "/v1/projects", json={"title": "XLink Project", "description": "x"}
    )
    assert proj.status_code == 201, proj.text
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)
        # Sibling page that already exists with a distinctive title.
        await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Knowledge Distillation",
            slug=None,
            page_kind="topic",
            body_md="# Knowledge Distillation\n\nTransfer from teacher to student.\n",
            summary="kd",
            claims=[],
            wikilinks=[],
            commit_message="seed sibling",
        )
        # New page mentions the sibling in PROSE only (no [[ ]] markup, no link).
        new = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Student Models",
            slug=None,
            page_kind="topic",
            body_md="# Student Models\n\nStudent models are trained via "
            "Knowledge Distillation from a teacher.\n",
            summary="student",
            claims=[],
            wikilinks=[],
            commit_message="seed new",
        )
        await session.commit()
        new_page_id = new.page_id

    # Pre-condition: the new page has no outgoing link yet.
    async with maker() as session:
        links = (
            (await session.execute(select(WikiLink).where(WikiLink.src_page_id == new_page_id)))
            .scalars()
            .all()
        )
        assert len(list(links)) == 0

    # Run the curator for the new page.
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        result = await CuratorService(session, ledger=LedgerWriter(session), actor_id=owner).curate(
            project_id=pid, page_id=new_page_id
        )
        await session.commit()

    assert result.cross_links_added >= 1

    # Post-condition: the new page now links to the sibling, resolved.
    async with maker() as session:
        link = (
            await session.execute(
                select(WikiLink).where(
                    WikiLink.src_page_id == new_page_id,
                    WikiLink.dst_title == "Knowledge Distillation",
                )
            )
        ).scalar_one()
        assert link.dst_page_id is not None

    # Idempotent: a second curate adds no new cross-links.
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        again = await CuratorService(session, ledger=LedgerWriter(session), actor_id=owner).curate(
            project_id=pid, page_id=new_page_id
        )
        await session.commit()
    assert again.cross_links_added == 0


async def test_cross_link_preserves_page_claims(http_client, auth_bypass, asgi_app, monkeypatch):
    """Cross-linking a page must NOT drop its claims/citations (provenance regression)."""
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import WikiClaim, WikiPage
    from aleph_wiki.wiki_service import ClaimDraft, WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post(
        "/v1/projects", json={"title": "Claims XLink", "description": "x"}
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
        await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Teacher Networks",
            slug=None,
            page_kind="topic",
            body_md="# Teacher Networks\n\nThe teacher.\n",
            summary="t",
            claims=[],
            wikilinks=[],
            commit_message="seed sibling",
        )
        # New page HAS a claim AND mentions the sibling in prose.
        new = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Student Networks",
            slug=None,
            page_kind="topic",
            body_md="# Student Networks\n\nA student learns from Teacher Networks.\n",
            summary="s",
            claims=[ClaimDraft(text="Students learn from teachers.", confidence="cited")],
            wikilinks=[],
            commit_message="seed new",
        )
        await session.commit()
        new_page_id = new.page_id

    # Pre: the page has 1 claim on its current revision.
    async with maker() as session:
        page = (
            await session.execute(select(WikiPage).where(WikiPage.id == new_page_id))
        ).scalar_one()
        claims_before = (
            (
                await session.execute(
                    select(WikiClaim).where(WikiClaim.revision_id == page.current_revision_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(list(claims_before)) == 1

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        result = await CuratorService(session, ledger=LedgerWriter(session), actor_id=owner).curate(
            project_id=pid, page_id=new_page_id
        )
        await session.commit()
    assert result.cross_links_added >= 1  # the prose mention got linked

    # Post: the cross-linked NEW current revision STILL carries the claim.
    async with maker() as session:
        page = (
            await session.execute(select(WikiPage).where(WikiPage.id == new_page_id))
        ).scalar_one()
        claims_after = (
            (
                await session.execute(
                    select(WikiClaim).where(WikiClaim.revision_id == page.current_revision_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(list(claims_after)) == 1, (
            "cross_link dropped the page's claims (provenance regression)"
        )
