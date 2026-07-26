"""Integration: artifact drift (WP-6 §5 / F4 item 4).

An artifact records the exact revision of each contributing wiki page in
`ArtifactVersion.lineage_jsonb["source_pages"]` at build time. Committing a
newer revision to a contributing page makes the artifact compute `drifted=true`
(live-computed by the Library surface builder — no stored flag).
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
        return {"sub": "user-drift", "email": "drift@test.local", "name": "Drift"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _artifact_drifted(http_client, pid: UUID, artifact_id: UUID) -> bool:
    """Read the Library surface and pull the artifact's live `drifted` flag."""
    resp = await http_client.get(f"/v1/projects/{pid}/surfaces/library")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for msg in body["messages"]:
        udm = msg.get("updateDataModel")
        if not udm:
            continue
        value = udm.get("value")
        if not isinstance(value, dict):
            continue
        for a in value.get("artifacts") or []:
            if a.get("id") == str(artifact_id):
                return bool(a.get("drifted"))
    raise AssertionError(f"artifact {artifact_id} not found in library surface")


async def test_artifact_drift_flips_when_upstream_page_moves(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_artifacts.artifact_service import create_artifact
    from aleph_artifacts.models import ArtifactVersion
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.models import WikiPage
    from aleph_wiki.wiki_service import WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post("/v1/projects", json={"title": "Drift", "description": "x"})
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)
        page = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Drift Topic",
            slug=None,
            page_kind="topic",
            body_md="# Drift Topic\n\nv1\n",
            summary="v1",
            claims=[],
            wikilinks=[],
            commit_message="rev1",
        )
        await session.commit()
        wp = await session.get(WikiPage, page.page_id)
        assert wp is not None
        built_rev = wp.current_revision_id

        # An artifact whose current version recorded the page at rev1.
        artifact = await create_artifact(
            session,
            principal=principal,
            project_id=pid,
            title="Report",
            artifact_kind="report_markdown_bundle",
        )
        event = await ledger.append(
            project_id=pid,
            actor_id=owner,
            actor_kind="user",
            action_kind="artifact.version.create",
            target_id=artifact.id,
            target_kind="artifact",
            payload={"seed": True},
            trace_id=None,
        )
        version = ArtifactVersion(
            id=uuid7(),
            project_id=pid,
            artifact_id=artifact.id,
            version_no=1,
            storage_uri="mem://seed",
            bytes_size=1,
            sha256="0" * 64,
            parent_version_id=None,
            lineage_jsonb={
                "source_pages": [
                    {
                        "page_id": str(page.page_id),
                        "revision_id": str(built_rev),
                        "revision_created_at": None,
                    }
                ]
            },
            template_name="default",
            csl_style="apa-7",
            builder_agent_run_id=uuid7(),
            author_kind="user",
            author_id=owner,
            ledger_event_id=event.id,
        )
        session.add(version)
        await session.flush()
        artifact.current_version_id = version.id
        await session.commit()
        artifact_id = artifact.id

    # Before any upstream change: not drifted.
    assert await _artifact_drifted(http_client, pid, artifact_id) is False

    # Commit a NEWER revision to the contributing page.
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        await WikiService(session).commit_revision(
            principal=principal,
            ledger=LedgerWriter(session),
            project_id=pid,
            page_id=page.page_id,
            title="Drift Topic",
            slug=None,
            page_kind="topic",
            body_md="# Drift Topic\n\nv2 — changed\n",
            summary="v2",
            claims=[],
            wikilinks=[],
            commit_message="rev2",
        )
        await session.commit()

    # The artifact now drifts (its recorded revision is stale).
    assert await _artifact_drifted(http_client, pid, artifact_id) is True
