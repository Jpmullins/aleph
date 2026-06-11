"""The `open` card action resolves its target to a navigable location.

Clicking "Open"/"View" on cards used to echo the target back and the frontend
discarded it — you could not read a synthesis proposal before approving it.
`_open` now resolves the target server-side to `{navigate: {tab, page_id?}}`
so the UI can switch the right panel to the thing being opened.
"""

from __future__ import annotations

from uuid import UUID

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-open", "email": "open@test.local", "name": "Open"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_open_synthesis_proposal_navigates_to_draft_page(http_client, auth_bypass):
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Open nav", "description": "t", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    project_id = UUID(proj.json()["id"])

    # A real pending proposal via note-promote (draft page + SynthesisProposal).
    note = await http_client.post(
        f"/v1/projects/{project_id}/notes", json={"title": "Promote me"}
    )
    note_id = note.json()["id"]
    sec = await http_client.post(
        f"/v1/projects/{project_id}/notes/{note_id}/sections",
        json={"body_md": "A finding worth a wiki page."},
    )
    assert sec.status_code == 201, sec.text
    promoted = await http_client.post(f"/v1/projects/{project_id}/notes/{note_id}/promote")
    assert promoted.status_code == 202, promoted.text
    proposal_id = promoted.json()["proposal_id"]
    page_id = promoted.json()["page_id"]

    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "open",
            "target_id": proposal_id,
            "target_kind": "synthesis_proposal",
            "params": {"target_id": proposal_id, "target_kind": "synthesis_proposal"},
        },
    )
    assert resp.status_code == 200, resp.text
    nav = resp.json()["result"]["navigate"]
    assert nav["tab"] == "Wiki"
    assert nav["page_id"] == page_id


async def test_open_hypothesis_navigates_to_hypotheses_tab(http_client, auth_bypass):
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Open nav hyp", "description": "t", "budget_usd": "5.00"},
    )
    project_id = UUID(proj.json()["id"])
    hyp = await http_client.post(
        f"/v1/projects/{project_id}/hypotheses",
        json={"title": "H", "statement": "s"},
    )
    assert hyp.status_code == 201, hyp.text
    hyp_id = hyp.json()["id"]

    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "chat",
            "action_kind": "open",
            "target_id": hyp_id,
            "target_kind": "hypothesis",
            "params": {"target_id": hyp_id, "target_kind": "hypothesis"},
        },
    )
    assert resp.status_code == 200, resp.text
    nav = resp.json()["result"]["navigate"]
    assert nav["tab"] == "Hypotheses"
