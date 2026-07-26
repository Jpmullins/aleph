"""WP-4d — agent composition verbs through the ledger-audited action router.

`compose_dossier` and `spotlight` are new card actions the Live agent calls to
build up the Briefs workbench. They must (like every card action) run through
the one ActionRouter: validate params, mutate state, write a ledger event and a
CardAction row. `compose_dossier` persists a derived/read-only WikiPageCard that
appears in Briefs; `spotlight` flips a persisted bit that orders the card first.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-wp4d", "email": "wp4d@test.local", "name": "WP4d"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _make_project(http_client) -> UUID:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "WP4d hands", "description": "t"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _pin_claim(http_client, project_id: UUID, *, text: str) -> UUID:
    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/pin",
        json={
            "card_kind": "ClaimCard",
            "title": text,
            "props": {
                "claim_id": str(uuid4()),
                "text": text,
                "confidence": "well-supported",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["card_id"])


def _children(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for m in messages:
        body = m.get("updateComponents")
        if body:
            for c in body.get("components", []):
                if c.get("id") == "root":
                    return c.get("children", [])
    return []


async def test_compose_dossier_persists_derived_card_and_ledgers(
    http_client, auth_bypass, asgi_app
):
    project_id = await _make_project(http_client)
    grouped = await _pin_claim(http_client, project_id, text="A grouped claim")

    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "compose_dossier",
            "params": {"title": "Q3 dossier", "card_ids": [str(grouped)]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["derived"] is True and result["read_only"] is True
    assert result["card_count"] == 1
    dossier_id = UUID(result["card_id"])

    from aleph_a2ui.models import CardAction, InteractiveCard, InteractiveCardVersion
    from aleph_db.models.ledger import ActionLedgerEvent

    maker = asgi_app.state.session_maker
    async with maker() as session:
        card = await session.get(InteractiveCard, dossier_id)
        assert card is not None
        assert card.card_kind == "WikiPageCard"
        assert card.pinned_to == "briefs"
        version = await session.get(InteractiveCardVersion, card.current_version_id)
        assert version is not None
        props = version.a2ui_payload_jsonb["props"]
        assert props["derived"] is True and props["read_only"] is True
        assert props["dossier_refs"]["card_ids"] == [str(grouped)]

        compose_events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == project_id,
                        ActionLedgerEvent.action_kind == "card.compose_dossier",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(compose_events) == 1 and compose_events[0].target_id == dossier_id

        actions = list(
            (
                await session.execute(
                    select(CardAction).where(
                        CardAction.project_id == project_id,
                        CardAction.action_kind == "compose_dossier",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(actions) == 1 and actions[0].ledger_event_id is not None

    # The dossier renders in Briefs as a read-only WikiPageCard.
    surf = await http_client.get(f"/v1/projects/{project_id}/surfaces/briefs")
    children = _children(surf.json()["messages"])
    dossiers = [
        c
        for c in children
        if c.get("type") == "WikiPageCard" and c.get("props", {}).get("read_only") is True
    ]
    assert len(dossiers) == 1


async def test_spotlight_bit_persists_and_orders_first_in_briefs(
    http_client, auth_bypass, asgi_app
):
    project_id = await _make_project(http_client)
    first_pinned = await _pin_claim(http_client, project_id, text="Ordinary claim")
    spotlit = await _pin_claim(http_client, project_id, text="The important claim")

    resp = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "briefs",
            "action_kind": "spotlight",
            "params": {"card_id": str(spotlit)},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["spotlighted"] is True

    from aleph_a2ui.models import InteractiveCard
    from aleph_db.models.ledger import ActionLedgerEvent

    maker = asgi_app.state.session_maker
    async with maker() as session:
        card = await session.get(InteractiveCard, spotlit)
        assert card is not None and card.spotlighted is True
        other = await session.get(InteractiveCard, first_pinned)
        assert other is not None and other.spotlighted is False
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == project_id,
                        ActionLedgerEvent.action_kind == "card.spotlight",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1 and events[0].target_id == spotlit

    # Spotlighted card sorts to the front of the Briefs pile with a flag.
    surf = await http_client.get(f"/v1/projects/{project_id}/surfaces/briefs")
    children = _children(surf.json()["messages"])
    claim_cards = [c for c in children if c.get("type") == "ClaimCard"]
    assert claim_cards, "expected pinned ClaimCards in Briefs"
    assert claim_cards[0]["props"].get("spotlight") is True
    assert claim_cards[0]["props"]["text"] == "The important claim"
