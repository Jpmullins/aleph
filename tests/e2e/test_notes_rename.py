"""Notes can be renamed: PATCH /notes/{note_id} updates the title + ledgers it."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-notes", "email": "notes@test.local", "name": "Notes"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _make_project_and_note(http_client) -> tuple[UUID, UUID]:
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Notes rename", "description": "t"},
    )
    assert proj.status_code == 201, proj.text
    project_id = UUID(proj.json()["id"])
    note = await http_client.post(
        f"/v1/projects/{project_id}/notes", json={"title": "Untitled note"}
    )
    assert note.status_code == 201, note.text
    return project_id, UUID(note.json()["id"])


async def test_patch_note_renames_and_ledgers(http_client, auth_bypass, asgi_app):
    project_id, note_id = await _make_project_and_note(http_client)

    resp = await http_client.patch(
        f"/v1/projects/{project_id}/notes/{note_id}",
        json={"title": "Sandworm infra timeline"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Sandworm infra timeline"

    # Persisted + visible in the list.
    listing = await http_client.get(f"/v1/projects/{project_id}/notes")
    titles = [n["title"] for n in listing.json()]
    assert "Sandworm infra timeline" in titles

    # Ledger event in the same mutation (rule #4).
    from aleph_db.models.ledger import ActionLedgerEvent

    maker = asgi_app.state.session_maker
    async with maker() as session:
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == project_id,
                        ActionLedgerEvent.action_kind == "note.update",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].target_id == note_id
    assert events[0].payload_jsonb.get("title") == "Sandworm infra timeline"


async def test_patch_note_unknown_id_is_404(http_client, auth_bypass):
    project_id, _ = await _make_project_and_note(http_client)
    resp = await http_client.patch(
        f"/v1/projects/{project_id}/notes/{uuid4()}",
        json={"title": "nope"},
    )
    assert resp.status_code == 404, resp.text
