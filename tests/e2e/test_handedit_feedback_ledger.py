"""Hand-edit mark/clear + rejection-feedback each write an ActionLedgerEvent."""

from __future__ import annotations

from uuid import UUID

import pytest

pytestmark = pytest.mark.integration


async def _ledger_kinds(http_client, pid: str) -> list[str]:
    r = await http_client.get(f"/v1/projects/{pid}/ledger?limit=200")
    assert r.status_code == 200
    return [e["action_kind"] for e in r.json()]


async def test_handedit_and_feedback_write_ledger(http_client, asgi_app, monkeypatch) -> None:
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_wiki.models import WikiPage

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)

    p = await http_client.post(
        "/v1/projects", json={"title": "HandeditFeedbackLedger", "description": "x"}
    )
    assert p.status_code == 201, p.text
    pid = p.json()["id"]

    # Seed a wiki page to hand-edit (no REST page-create route exists).
    maker = asgi_app.state.session_maker
    async with maker() as session:
        owner = (await session.get(Project, UUID(pid))).created_by
        page = WikiPage(
            id=uuid7(),
            project_id=UUID(pid),
            title="Topic",
            slug="topic",
            page_kind="topic",
            status="draft",
            created_by=owner,
        )
        session.add(page)
        await session.commit()
        page_id = page.id

    m = await http_client.post(f"/v1/projects/{pid}/wiki/pages/{page_id}/sections/content/handedit")
    assert m.status_code == 201, m.text
    c = await http_client.delete(
        f"/v1/projects/{pid}/wiki/pages/{page_id}/sections/content/handedit"
    )
    assert c.status_code == 204, c.text

    f = await http_client.post(
        f"/v1/projects/{pid}/wiki/feedback/rejection",
        json={"concept_name": "Topic", "reason": "wrong"},
    )
    assert f.status_code == 201, f.text

    kinds = await _ledger_kinds(http_client, pid)
    assert "wiki.handedit.mark" in kinds
    assert "wiki.handedit.clear" in kinds
    assert "wiki.feedback.write" in kinds

    v = await http_client.get(f"/v1/projects/{pid}/ledger/verify")
    assert v.json()["ok"] is True
