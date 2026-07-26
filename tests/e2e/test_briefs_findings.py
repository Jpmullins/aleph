"""Review findings surface in Briefs and are actionable (resolve / dismiss).

Regression target: reviewers write ReviewFinding rows that no surface ever
showed — the provenance loop ran headless. Open findings must render as
FindingCard children of the Briefs surface, count toward the badge, and the
approve/reject card actions must resolve/dismiss them with a ledger event.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-briefs", "email": "briefs@test.local", "name": "Briefs"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _make_project(http_client) -> UUID:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "Briefs findings", "description": "t"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _make_finding(asgi_app, project_id: UUID, *, title: str) -> UUID:
    from aleph_core.ids import uuid7
    from aleph_core.time import utcnow
    from aleph_db.models.project import Project
    from aleph_reviewer.models import ReviewFinding, ReviewRun

    maker = asgi_app.state.session_maker
    async with maker() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        owner = project.created_by
        run = ReviewRun(
            id=uuid7(),
            project_id=project_id,
            kind="mechanical",
            trigger="manual",
            agent_run_id=uuid7(),
            status="succeeded",
            started_at=utcnow(),
            created_by=owner,
            access_scope="project",
        )
        session.add(run)
        await session.flush()
        finding = ReviewFinding(
            id=uuid7(),
            project_id=project_id,
            review_run_id=run.id,
            finding_kind="broken_citation",
            severity="medium",
            title=title,
            description="Claim c12 cites a chunk that does not contain the claim.",
            created_by=owner,
            access_scope="project",
        )
        session.add(finding)
        await session.commit()
        return finding.id


def _children(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for m in messages:
        body = m.get("updateComponents")
        if body:
            for c in body.get("components", []):
                if c.get("id") == "root":
                    return c.get("children", [])
    return []


async def test_open_findings_render_in_briefs(http_client, auth_bypass, asgi_app):
    project_id = await _make_project(http_client)
    finding_id = await _make_finding(asgi_app, project_id, title="Broken citation on page X")

    resp = await http_client.get(f"/v1/projects/{project_id}/surfaces/briefs")
    assert resp.status_code == 200, resp.text
    children = _children(resp.json()["messages"])
    finding_cards = [c for c in children if c.get("type") == "FindingCard"]
    assert len(finding_cards) == 1
    assert finding_cards[0]["props"]["finding_id"] == str(finding_id)

    root = next(
        c
        for m in resp.json()["messages"]
        if (b := m.get("updateComponents"))
        for c in b["components"]
        if c.get("id") == "root"
    )
    assert root["badge_count"] == 1


async def test_approve_resolves_finding(http_client, auth_bypass, asgi_app):
    project_id = await _make_project(http_client)
    finding_id = await _make_finding(asgi_app, project_id, title="Resolvable finding")

    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "approve",
            "target_id": str(finding_id),
            "target_kind": "review_finding",
            "params": {"target_id": str(finding_id), "target_kind": "review_finding"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_reviewer.models import ReviewFinding

    maker = asgi_app.state.session_maker
    async with maker() as session:
        finding = await session.get(ReviewFinding, finding_id)
        assert finding is not None and finding.status == "resolved"
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == project_id,
                        ActionLedgerEvent.action_kind == "review_finding.resolve",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].target_id == finding_id

    # Resolved findings leave the Briefs pile.
    surf = await http_client.get(f"/v1/projects/{project_id}/surfaces/briefs")
    assert all(c.get("type") != "FindingCard" for c in _children(surf.json()["messages"]))


async def test_reject_dismisses_finding(http_client, auth_bypass, asgi_app):
    project_id = await _make_project(http_client)
    finding_id = await _make_finding(asgi_app, project_id, title="Dismissable finding")

    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "reject",
            "target_id": str(finding_id),
            "target_kind": "review_finding",
            "params": {
                "target_id": str(finding_id),
                "target_kind": "review_finding",
                "reason": "not a real problem",
            },
        },
    )
    assert resp.status_code == 200, resp.text

    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_reviewer.models import ReviewFinding

    maker = asgi_app.state.session_maker
    async with maker() as session:
        finding = await session.get(ReviewFinding, finding_id)
        assert finding is not None and finding.status == "dismissed"
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == project_id,
                        ActionLedgerEvent.action_kind == "review_finding.dismiss",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
