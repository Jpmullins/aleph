"""Integration: wiki staleness refresh (WP-6 §3 / F4 item 2).

A fixture-aged source cited by a page → `wiki_refresh_job` emits exactly one
`refresh_result` ApprovalCard in Briefs. Approving (skip / re-affirm) bumps
`WikiPage.verified_at`; flagging (reject) downgrades the page's claims to
`contested`. Neither path recompiles the page (the current revision is
UNCHANGED). Also asserts `_wiki_messages` emits `page_meta.freshness`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-refresh", "email": "refresh@test.local", "name": "Refresh"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _seed_page_with_source(http_client, asgi_app, monkeypatch, *, title: str):
    """A source (+ SourceVersion + NormalizedDocument) cited by a claim on one
    topic page. Ages `verified_at` so the page reads as stale."""
    from aleph_core.ids import uuid7
    from aleph_core.time import utcnow
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_rks.models import NormalizedDocument, Source, SourceAsset, SourceVersion
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import SourcePage, WikiPage
    from aleph_wiki.wiki_service import CitationDraft, ClaimDraft, WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post(
        "/v1/projects", json={"title": title, "description": "x", "budget_usd": "1.00"}
    )
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker
    asset_store = asgi_app.state.asset_store

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)

        # The source's representation page (SourcePage.page_id).
        srcpage = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title=f"{title} Source Doc",
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
            title="Aging Source",
            url=None,
            short_id=uuid7().hex[:16],
            status="wiki_done",
            created_by=owner,
            access_scope="project",
        )
        session.add(source)
        await session.flush()

        # A stored NormalizedDocument so the refresh has a prior markdown to diff.
        asset = SourceAsset(
            id=uuid7(),
            project_id=pid,
            storage_uri="",
            mime_type="text/plain",
            size_bytes=8,
            sha256="0" * 64,
            created_by=owner,
            access_scope="project",
        )
        session.add(asset)
        version = SourceVersion(
            id=uuid7(),
            source_id=source.id,
            version_no=1,
            asset_id=asset.id,
            sha256="0" * 64,
            fetched_at=utcnow() - timedelta(days=400),
            created_by=owner,
            access_scope="project",
        )
        session.add(version)
        await session.flush()
        md_uri = asset_store.put_normalized_markdown(
            project_id=pid, source_id=source.id, version_no=1, markdown="old text"
        )
        nd = NormalizedDocument(
            id=uuid7(),
            project_id=pid,
            source_id=source.id,
            source_version_id=version.id,
            markdown_uri=md_uri,
            parser="text",
            parser_version="1",
            char_count=8,
            token_count=2,
            created_by=owner,
            access_scope="project",
        )
        session.add(nd)
        await session.flush()
        version.normalized_document_id = nd.id
        source.current_version_id = version.id

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

        cite = CitationDraft(chunk_ids=[], source_page_id=bridge.id, citation_marker="[1]")
        topic = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title=f"{title} Topic",
            slug=None,
            page_kind="topic",
            body_md="# Topic\n\nClaim [1].\n",
            summary="t",
            claims=[ClaimDraft(text="Relies on the source.", confidence="cited", citations=[cite])],
            wikilinks=[],
            commit_message="seed topic",
        )
        await session.commit()

        # Compute freshness deterministically + age verified_at into the past.
        await CuratorService(session, ledger=LedgerWriter(session), actor_id=owner).curate(
            project_id=pid, page_id=topic.page_id
        )
        page = await session.get(WikiPage, topic.page_id)
        assert page is not None
        page.verified_at = utcnow() - timedelta(days=400)
        await session.commit()

        current_rev = page.current_revision_id
    return pid, topic.page_id, current_rev, owner


def _ctx(asgi_app):
    return {
        "session_maker": asgi_app.state.session_maker,
        "asset_store": asgi_app.state.asset_store,
        "litellm_client": object(),  # unused: _classify_factdiff is stubbed
        "agent_token_secret": asgi_app.state.settings.aleph_agent_token_secret,
    }


def _token(asgi_app, pid: UUID, owner: UUID) -> str:
    from aleph_core.ids import uuid7
    from aleph_security.agent_token import mint_agent_token

    run_id = uuid7()
    return mint_agent_token(
        secret=asgi_app.state.settings.aleph_agent_token_secret,
        user_id=owner,
        project_id=pid,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id=f"wiki-refresh-{run_id.hex}",
        ttl_seconds=3600,
    )


async def _run_refresh(asgi_app, monkeypatch, pid, page_id, owner, *, verdict: str):
    import aleph_workers.jobs.wiki_refresh as mod

    async def fake_refetch(source, asset_store, *, asset):
        return "new text"

    async def fake_classify(**_):
        return verdict

    monkeypatch.setattr(mod, "_refetch_source_markdown", fake_refetch)
    monkeypatch.setattr(mod, "_classify_factdiff", fake_classify)
    return await mod.wiki_refresh_job(
        _ctx(asgi_app), str(pid), str(page_id), _token(asgi_app, pid, owner)
    )


async def _refresh_cards(asgi_app, pid):
    from aleph_a2ui.models import InteractiveCard

    async with asgi_app.state.session_maker() as session:
        cards = list(
            (
                await session.execute(
                    select(InteractiveCard).where(
                        InteractiveCard.project_id == pid,
                        InteractiveCard.card_kind == "ApprovalCard",
                        InteractiveCard.pinned_to == "briefs",
                    )
                )
            )
            .scalars()
            .all()
        )
        return cards


async def test_refresh_emits_card_and_approve_skip_reaffirms(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_wiki.models import WikiPage

    pid, page_id, current_rev, owner = await _seed_page_with_source(
        http_client, asgi_app, monkeypatch, title="RefreshA"
    )

    # Freshness is emitted by the wiki surface (F4 rendering).
    surf = await http_client.get(f"/v1/projects/{pid}/surfaces/wiki?page_id={page_id}")
    assert surf.status_code == 200
    assert "freshness" in surf.text

    out = await _run_refresh(asgi_app, monkeypatch, pid, page_id, owner, verdict="unchanged")
    assert out["ok"] is True

    cards = await _refresh_cards(asgi_app, pid)
    assert len(cards) == 1, "expected exactly one refresh_result ApprovalCard"

    # Approve (skip / re-affirm) via the card-action router.
    resp = await http_client.post(
        f"/v1/projects/{pid}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "approve",
            "params": {"target_id": str(page_id), "target_kind": "refresh_result"},
        },
    )
    assert resp.status_code == 200, resp.text

    async with asgi_app.state.session_maker() as session:
        page = (await session.execute(select(WikiPage).where(WikiPage.id == page_id))).scalar_one()
        assert page.verified_at is not None
        # Re-affirmed to ~now (the aged verified_at was 400d ago).
        from aleph_core.time import utcnow

        assert (utcnow() - page.verified_at) < timedelta(days=1)
        # NEVER recompiled.
        assert page.current_revision_id == current_rev


async def test_refresh_flag_downgrades_claims_without_recompile(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_wiki.models import WikiClaim, WikiPage

    pid, page_id, current_rev, owner = await _seed_page_with_source(
        http_client, asgi_app, monkeypatch, title="RefreshB"
    )

    out = await _run_refresh(asgi_app, monkeypatch, pid, page_id, owner, verdict="contradicted")
    assert out["ok"] is True
    assert out["worst"] == "contradicted"

    # Flag (reject) via the router.
    resp = await http_client.post(
        f"/v1/projects/{pid}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "reject",
            "params": {
                "target_id": str(page_id),
                "target_kind": "refresh_result",
                "reason": "source contradicts the page",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["claims_contested"] >= 1

    async with asgi_app.state.session_maker() as session:
        page = (await session.execute(select(WikiPage).where(WikiPage.id == page_id))).scalar_one()
        assert page.current_revision_id == current_rev, "flag must NOT recompile the page"
        claims = list(
            (await session.execute(select(WikiClaim).where(WikiClaim.revision_id == current_rev)))
            .scalars()
            .all()
        )
        assert claims
        assert all(c.confidence == "contested" for c in claims)
        assert all(c.status == "contested" for c in claims)
