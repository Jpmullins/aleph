"""Recent card actions are queryable — the agent's feedback loop.

Card actions (approve/reject/unpin/...) were fire-and-forget from the agent's
perspective: the ActionRouter recorded CardAction rows nothing ever read back.
GET /cards/actions exposes the recent ones so the chat surface can share
"what the analyst just did" with the agent as context.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHART_PROPS: dict[str, Any] = {
    "dataset_version_id": None,
    "title": "Feed test chart",
    "vega_lite_spec": {"mark": "bar"},
    "open_action": "open",
    "_placeholder": True,
}


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-feed", "email": "feed@test.local", "name": "Feed"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_recent_card_actions_listed(http_client, auth_bypass):
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Actions feed", "description": "t"},
    )
    assert proj.status_code == 201, proj.text
    project_id = UUID(proj.json()["id"])

    pin = await http_client.post(
        f"/v1/projects/{project_id}/cards/pin",
        json={"card_kind": "ChartCard", "title": "Feed test chart", "props": CHART_PROPS},
    )
    assert pin.status_code == 201, pin.text
    card_id = pin.json()["card_id"]

    act = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "unpin",
            "card_id": card_id,
            "params": {"card_id": card_id},
        },
    )
    assert act.status_code == 200, act.text

    resp = await http_client.get(f"/v1/projects/{project_id}/cards/actions?limit=10")
    assert resp.status_code == 200, resp.text
    actions = resp.json()
    assert len(actions) >= 1
    newest = actions[0]
    assert newest["action_kind"] == "unpin"
    assert newest["surface_kind"] == "briefs"
    assert newest["result"]["card_id"] == card_id
    assert "created_at" in newest
