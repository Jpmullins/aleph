"""Pin-to-Briefs: agent-built cards persist as InteractiveCards in the pile.

The Inc-4 InteractiveCard model has carried `pinned_to`/`pinned_target_id`
unused since it landed; chat-inline cards (e.g. viz_builder charts) were
ephemeral chat state. POST /cards/pin persists a catalog-validated card +
immutable version, Briefs renders pinned cards, and the `unpin` action
removes them — all ledgered.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHART_PROPS = {
    "dataset_version_id": None,
    "title": "GPU price trend",
    "vega_lite_spec": {"mark": "line", "data": {"values": [{"x": 1, "y": 2}]}},
    "open_action": "open",
    "_placeholder": True,
}


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-pin", "email": "pin@test.local", "name": "Pin"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _make_project(http_client) -> UUID:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "Card pinning", "description": "t", "budget_usd": "5.00"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


def _children(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for m in messages:
        body = m.get("updateComponents")
        if body:
            for c in body.get("components", []):
                if c.get("id") == "root":
                    return c.get("children", [])
    return []


async def test_pin_chart_card_to_briefs(http_client, auth_bypass, asgi_app):
    project_id = await _make_project(http_client)

    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/pin",
        json={"card_kind": "ChartCard", "title": "GPU price trend", "props": CHART_PROPS},
    )
    assert resp.status_code == 201, resp.text
    card_id = UUID(resp.json()["card_id"])

    from aleph_a2ui.models import InteractiveCard, InteractiveCardVersion
    from aleph_db.models.ledger import ActionLedgerEvent

    maker = asgi_app.state.session_maker
    async with maker() as session:
        card = await session.get(InteractiveCard, card_id)
        assert card is not None
        assert card.pinned_to == "briefs"
        assert card.card_kind == "ChartCard"
        version = (
            await session.execute(
                select(InteractiveCardVersion).where(InteractiveCardVersion.card_id == card_id)
            )
        ).scalar_one()
        assert version.version_no == 1
        assert version.a2ui_payload_jsonb["type"] == "ChartCard"
        assert card.current_version_id == version.id
        pin_events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == project_id,
                        ActionLedgerEvent.action_kind == "card.pin",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(pin_events) == 1
    assert pin_events[0].target_id == card_id

    # Renders in Briefs (and counts toward the badge).
    surf = await http_client.get(f"/v1/projects/{project_id}/surfaces/briefs")
    children = _children(surf.json()["messages"])
    pinned = [c for c in children if c.get("id") == f"pinned-{card_id}"]
    assert len(pinned) == 1
    assert pinned[0]["type"] == "ChartCard"
    assert pinned[0]["props"]["title"] == "GPU price trend"


async def test_pin_rejects_unknown_component(http_client, auth_bypass):
    project_id = await _make_project(http_client)
    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/pin",
        json={"card_kind": "EvilCard", "title": "x", "props": {}},
    )
    assert resp.status_code == 422, resp.text


async def test_unpin_action_removes_from_briefs(http_client, auth_bypass, asgi_app):
    project_id = await _make_project(http_client)
    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/pin",
        json={"card_kind": "ChartCard", "title": "Unpin me", "props": CHART_PROPS},
    )
    assert resp.status_code == 201, resp.text
    card_id = resp.json()["card_id"]

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

    from aleph_a2ui.models import InteractiveCard
    from aleph_db.models.ledger import ActionLedgerEvent

    maker = asgi_app.state.session_maker
    async with maker() as session:
        card = await session.get(InteractiveCard, UUID(card_id))
        assert card is not None and card.pinned_to is None
        unpin_events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == project_id,
                        ActionLedgerEvent.action_kind == "card.unpin",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(unpin_events) == 1

    surf = await http_client.get(f"/v1/projects/{project_id}/surfaces/briefs")
    assert all(c.get("id") != f"pinned-{card_id}" for c in _children(surf.json()["messages"]))
