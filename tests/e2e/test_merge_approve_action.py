"""Merge proposal approve/reject via the /cards/actions router (audit F05)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-mergeact", "email": "mergeact@test.local", "name": "MergeAct"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _seed_merge(http_client, asgi_app, monkeypatch):
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.models import PageMergeProposal
    from aleph_wiki.wiki_service import WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post(
        "/v1/projects", json={"title": "MergeAct", "description": "x", "budget_usd": "1.00"}
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
        src = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Dup A",
            slug=None,
            page_kind="topic",
            body_md="# Dup A\n\nx\n",
            summary="a",
            claims=[],
            wikilinks=[],
            commit_message="seed",
        )
        tgt = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Dup B",
            slug=None,
            page_kind="topic",
            body_md="# Dup B\n\ny\n",
            summary="b",
            claims=[],
            wikilinks=[],
            commit_message="seed",
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
        return pid, prop.id, src.page_id


async def test_merge_proposal_surfaces_in_briefs_and_approves(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_wiki.models import WikiPage

    pid, prop_id, src_id = await _seed_merge(http_client, asgi_app, monkeypatch)

    # It surfaces on the Briefs tab as an ApprovalCard.
    briefs = await http_client.get(f"/v1/projects/{pid}/surfaces/briefs")
    assert briefs.status_code == 200
    assert f"merge-{prop_id}" in briefs.text
    assert "page_merge_proposal" in briefs.text

    # Approve it through the card-action router.
    resp = await http_client.post(
        f"/v1/projects/{pid}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "approve",
            "params": {"target_id": str(prop_id), "target_kind": "page_merge_proposal"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # The source page is soft-deleted and the proposal approved.
    maker = asgi_app.state.session_maker
    async with maker() as session:
        from aleph_wiki.models import PageMergeProposal

        src = (await session.execute(select(WikiPage).where(WikiPage.id == src_id))).scalar_one()
        assert src.status == "deleted"
        prop = (
            await session.execute(select(PageMergeProposal).where(PageMergeProposal.id == prop_id))
        ).scalar_one()
        assert prop.status == "approved"


async def test_merge_proposal_reject_via_action(http_client, auth_bypass, asgi_app, monkeypatch):
    pid, prop_id, _src_id = await _seed_merge(http_client, asgi_app, monkeypatch)
    resp = await http_client.post(
        f"/v1/projects/{pid}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "reject",
            "params": {
                "target_id": str(prop_id),
                "target_kind": "page_merge_proposal",
                "reason": "not a duplicate",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    maker = asgi_app.state.session_maker
    async with maker() as session:
        from aleph_wiki.models import PageMergeProposal

        prop = (
            await session.execute(select(PageMergeProposal).where(PageMergeProposal.id == prop_id))
        ).scalar_one()
        assert prop.status == "rejected"
