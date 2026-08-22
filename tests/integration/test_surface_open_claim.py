"""'Open claim' resolves to the Grounding pane, carrying the claim.

WS-UI-4 c1. `ClaimCard` has emitted `open {target_kind: "claim"}` since it was
written and `_open` had no `claim` branch, so the result carried no `tab`, the
browser's `adapt()` ignored it, and the button was decoration. The claim ->
citation -> chunk -> char-span chain this project is built on had no route in
from the UI at all.

Driven through `ActionRouter.dispatch` against Postgres rather than by calling
`_open` directly: the router is what validates the params against the catalog
schema, collects the handler kwargs and writes the ledger row, and a handler
that is correct but unregistered — or registered under a params schema that
rejects its own caller — fails in exactly the way this test exists to catch.

The browser half is `apps/web/src/a2ui/navigate.test.tsx`, which asserts the
same `{tab, params: {claim_id}}` shape turns into the pane `grounding:claim_id=…`.
The two halves meet at that literal shape; changing it here without changing it
there is what this pair is for.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_a2ui.action_router import CardActionRequest
from aleph_a2ui.pane_registry import PANE_REGISTRY
from aleph_api.a2ui_handlers import build_action_router
from aleph_api.routes.surfaces import _parse_pane_specs
from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal
from aleph_wiki.models import WikiClaim

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _principal() -> Principal:
    return Principal(
        user_id=ACTOR, subject="open-claim", email="oc@example.test", actor_kind="user"
    )


async def _seed_claim(
    maker: Callable[[], AsyncSession], project_id: uuid.UUID, text: str
) -> uuid.UUID:
    claim_id = uuid7()
    async with maker() as session:
        session.add(
            WikiClaim(
                id=claim_id,
                project_id=project_id,
                page_id=uuid7(),
                text=text,
                claim_key=None,
                origin="agent",
                evidence_tier="stated",
                confidence="weakly_supported",
                status="active",
                created_by=ACTOR,
            )
        )
        await session.commit()
    return claim_id


async def _open_claim(
    maker: Callable[[], AsyncSession], project_id: uuid.UUID, claim_id: uuid.UUID
) -> dict[str, Any]:
    router = build_action_router()
    request = CardActionRequest(
        action_kind="open",
        surface_kind="wikiSurface",
        card_id=None,
        target_id=claim_id,
        target_kind="claim",
        # Exactly what `ClaimCard.tsx` posts.
        params={"target_id": str(claim_id), "target_kind": "claim"},
    )
    async with maker() as session:
        out = await router.dispatch(
            session=session,
            ledger=LedgerWriter(session),
            principal=_principal(),
            project_id=project_id,
            request=request,
        )
        await session.commit()
    return out.result


async def test_opening_a_claim_names_the_grounding_pane_and_its_parameter(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    claim_id = await _seed_claim(maker, committed_project, "Chunks are written before the embed.")

    nav = (await _open_claim(maker, committed_project, claim_id))["navigate"]

    assert nav["target_kind"] == "claim"
    # A pane, by a name the registry actually has once normalised — the client
    # lower-cases it into the wire `surfaceId`.
    assert nav["tab"].lower() in PANE_REGISTRY.ids()
    # And the parameter, which is the whole point: grounding is launchable=False
    # precisely because naming the pane without it opens an empty surface.
    assert nav["params"] == {"claim_id": str(claim_id)}


async def test_the_navigate_result_round_trips_through_the_pane_parser(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The client turns this result into a pane spec; the server must accept it.

    `_parse_pane_specs` DROPS a param the pane did not declare, silently and
    correctly. So a `params` key that does not match `PaneKind.params` produces
    a grounding pane that ignores its only argument and renders as though the
    claim had no evidence — the one thing that surface exists to distinguish.
    That is a rename away at all times, and nothing else checks it.
    """
    claim_id = await _seed_claim(maker, committed_project, "A claim with a route in.")
    nav = (await _open_claim(maker, committed_project, claim_id))["navigate"]

    # Exactly what `paneKey()` builds from `{tab, params}`.
    kind = str(nav["tab"]).lower()
    joined = "&".join(f"{k}={v}" for k, v in sorted(nav["params"].items()))
    specs = _parse_pane_specs(f"{kind}:{joined}")

    assert len(specs) == 1, f"the server dropped the pane spec the client would send: {specs}"
    surface_id, tab, params = specs[0]
    assert tab == kind
    assert params == {"claim_id": str(claim_id)}
    assert surface_id == f"{kind}:claim_id={claim_id}"


async def test_a_claim_from_another_project_is_refused_rather_than_opened_empty(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
) -> None:
    """A grounding pane for a claim that is not there renders as an ungrounded
    claim. Refusing is the only answer that is not a lie."""
    foreign = await _seed_claim(maker, second_project, "Someone else's claim.")

    with pytest.raises(NotFound):
        await _open_claim(maker, committed_project, foreign)


async def test_a_wiki_page_still_opens_the_way_it_did(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The older `page_id`-beside-the-tab shape is still what the reader needs."""
    router = build_action_router()
    page_id = uuid7()
    async with maker() as session:
        out = await router.dispatch(
            session=session,
            ledger=LedgerWriter(session),
            principal=_principal(),
            project_id=committed_project,
            request=CardActionRequest(
                action_kind="open",
                surface_kind="wikiSurface",
                card_id=None,
                target_id=page_id,
                target_kind="wiki_page",
                params={"target_id": str(page_id), "target_kind": "wiki_page"},
            ),
        )
        await session.commit()
    nav = out.result["navigate"]
    assert nav["tab"].lower() in PANE_REGISTRY.ids()
    assert nav["page_id"] == str(page_id)
