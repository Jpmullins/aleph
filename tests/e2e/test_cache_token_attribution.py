"""Cached and cache-written tokens must reach `ModelCall` + `CostLedgerEvent`.

The pricing arithmetic is unit-tested (`test_cache_write_pricing.py`). What is
NOT provable there is that the numbers survive the whole path: gateway response
→ `LiteLLMClient` usage parsing → `PricingTable.cost_for` → the two ledger rows
that rule #5 requires. A break anywhere in that chain is silent — the call still
succeeds, the rows still appear, and only the amounts are wrong.

Per END-STATE.md's fixture rule, the *boundary* is faked (a canned gateway
response, exactly as `test_smoke_llm.py` does) and nothing downstream is: the
profile lookup, the client, the pricing table, and both DB writes are the real
production objects.

This is what makes prompt caching safe to switch on. Without it, enabling
caching would move `cost_usd` in the reassuring direction with nothing checking
whether the movement is real.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: A model the real gateway actually serves. The previous value here
#: (`claude-haiku-4-5`) came from a hand-written price table and does not exist
#: on any gateway Aleph has met — the chain this file exercises would have
#: 400'd in production while passing here against a canned response.
MODEL = "bedrock-claude-opus-4.7"

#: The Insights gateway's own published rates, read from the captured
#: `/model/info` fixture below rather than asserted from memory.
IN_RATE = Decimal("5.5E-6")
OUT_RATE = Decimal("2.75E-5")
CACHE_READ_RATE = Decimal("5.5E-7")
CACHE_WRITE_RATE = Decimal("6.875E-6")

GATEWAY_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1].parent
    / "packages/aleph-models/tests/fixtures/insights_model_info.json"
)


def _discovered_pricing():
    """The real production construction: rates parsed from a gateway payload."""
    from aleph_models.discovery import parse_model_info
    from aleph_models.pricing import PricingTable

    return PricingTable.from_discovery(parse_model_info(json.loads(GATEWAY_FIXTURE.read_text())))


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "cache-attr", "email": "cache@test.local", "name": "Cache"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


def _gateway(monkeypatch, *, prompt_tokens, cached, written, completion):
    """Canned gateway response reporting a cache read AND a cache write."""
    from aleph_models import client as client_mod

    usage: dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion,
        "total_tokens": prompt_tokens + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }
    if written:
        # The spelling Anthropic uses and LiteLLM passes through.
        usage["cache_creation_input_tokens"] = written

    canned = {
        "id": "chatcmpl-cache",
        "model": MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": usage,
    }

    async def fake_post(self, path, payload):
        if path == "/v1/chat/completions":
            return canned
        msg = f"unexpected gateway path {path}"
        raise AssertionError(msg)

    monkeypatch.setattr(client_mod.LiteLLMClient, "_post_with_retry", fake_post)


async def _project(asgi_app, http_client) -> str:
    resp = await http_client.post(
        "/v1/projects", json={"title": f"cache-attr {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _chat(asgi_app, project_id: str):
    """Drive the REAL client through the REAL profile + pricing + writers."""
    from aleph_core.schemas.model_profile import Capability
    from aleph_db.repos import model_profile as profile_repo
    from aleph_models.client import ChatMessage
    from aleph_security.principal import Principal

    maker = asgi_app.state.session_maker
    from uuid import UUID

    pid = UUID(project_id)
    async with maker() as session:
        profile = await profile_repo.get_project_profile(session, pid)
    assert profile is not None

    # Production learns rates from the gateway at startup; this app instance
    # never contacted one, so fold the captured payload into the SAME
    # PricingTable object the client holds — exactly what
    # `GatewayCatalog.refresh_pricing` does at boot.
    asgi_app.state.pricing.merge(_discovered_pricing())
    bindings = dict(profile.bindings_jsonb)
    bindings[Capability.CLASSIFICATION.value] = {
        **bindings.get(Capability.CLASSIFICATION.value, {}),
        "model": MODEL,
    }

    principal = Principal(user_id=uuid4(), subject="cache-attr", email="c@t", actor_kind="user")
    return await asgi_app.state.litellm.chat(
        principal=principal,
        project_id=pid,
        agent_run_id=None,
        capability=Capability.CLASSIFICATION,
        profile_bindings=bindings,
        messages=[ChatMessage(role="user", content="hi")],
        purpose="test.cache_attribution",
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_rates_in_this_file_match_what_the_gateway_reports() -> None:
    """Guard the guard: if the gateway's rates move, this file's maths is stale.

    Previously this asserted against a table committed in the repo, which made
    it a check that two hand-written copies of a guess agreed. It now checks
    the arithmetic below against rates the gateway published.
    """
    table = _discovered_pricing()
    assert table.has(MODEL), f"the captured gateway payload does not serve {MODEL}"

    one_m = table.breakdown(
        model=MODEL, input_tokens=1_000_000, cached_tokens=0, completion_tokens=0
    )
    assert one_m.input_rate_usd == IN_RATE
    assert one_m.output_rate_usd == OUT_RATE
    reads = table.breakdown(model=MODEL, input_tokens=1000, cached_tokens=1000, completion_tokens=0)
    assert reads.cost_usd == (Decimal(1000) * CACHE_READ_RATE).quantize(Decimal("0.000001"))


async def test_cached_tokens_reach_the_model_call_row(
    asgi_app, http_client, auth_bypass, monkeypatch
):
    """`cached_tokens` must land in the DB, not just in the HTTP response."""
    from aleph_db.models.cost import ModelCall

    _gateway(monkeypatch, prompt_tokens=1000, cached=800, written=0, completion=10)
    project_id = await _project(asgi_app, http_client)
    resp = await _chat(asgi_app, project_id)

    from uuid import UUID

    async with asgi_app.state.session_maker() as session:
        call = (
            await session.execute(select(ModelCall).where(ModelCall.id == UUID(resp.model_call_id)))
        ).scalar_one()

    assert call.cached_tokens == 800, (
        f"gateway reported 800 cached tokens; ModelCall recorded "
        f"{call.cached_tokens}. Cache attribution is lost before the DB."
    )
    assert Decimal(call.cache_savings_usd) > 0, "a cache read produced no recorded saving"


async def test_cache_write_premium_is_billed_end_to_end(
    asgi_app, http_client, auth_bypass, monkeypatch
):
    """The headline: a cache-priming call must cost MORE, all the way to the row.

    Before the write premium existed, `cost_usd` here would have been strictly
    lower than the uncached equivalent — caching would have appeared to save
    money on the very call that costs extra.
    """
    from uuid import UUID

    from aleph_db.models.cost import ModelCall

    _gateway(monkeypatch, prompt_tokens=1000, cached=0, written=1000, completion=0)
    project_id = await _project(asgi_app, http_client)
    resp = await _chat(asgi_app, project_id)

    async with asgi_app.state.session_maker() as session:
        call = (
            await session.execute(select(ModelCall).where(ModelCall.id == UUID(resp.model_call_id)))
        ).scalar_one()

    uncached_equivalent = Decimal(1000) * IN_RATE
    expected = (Decimal(1000) * CACHE_WRITE_RATE).quantize(Decimal("0.000001"))
    assert Decimal(call.cost_usd) == expected, (
        f"cost_usd={call.cost_usd}, expected {expected} (1000 tokens at 1.25x). "
        f"The cache-write premium is not reaching the ledger."
    )
    assert Decimal(call.cost_usd) > uncached_equivalent, (
        "a cache-priming call was billed at or below the uncached rate — "
        "enabling caching would under-report spend."
    )


async def test_cost_ledger_event_matches_the_model_call(
    asgi_app, http_client, auth_bypass, monkeypatch
):
    """Rule #5: every LLM call writes BOTH rows, and they must agree.

    A `CostLedgerEvent` that disagrees with its `ModelCall` makes the project
    cost rollup and the per-call audit tell different stories.
    """
    from uuid import UUID

    from aleph_db.models.cost import CostLedgerEvent, ModelCall

    _gateway(monkeypatch, prompt_tokens=500, cached=200, written=100, completion=20)
    project_id = await _project(asgi_app, http_client)
    resp = await _chat(asgi_app, project_id)
    call_id = UUID(resp.model_call_id)

    async with asgi_app.state.session_maker() as session:
        call = (
            await session.execute(select(ModelCall).where(ModelCall.id == call_id))
        ).scalar_one()
        events = list(
            (
                await session.execute(
                    select(CostLedgerEvent).where(CostLedgerEvent.model_call_id == call_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 1, f"expected exactly one CostLedgerEvent, got {len(events)}"
    assert Decimal(events[0].cost_usd) == Decimal(call.cost_usd), (
        f"ledger event ({events[0].cost_usd}) disagrees with the model call "
        f"({call.cost_usd}) — the rollup and the audit would diverge."
    )

    # 200 read + 100 written + 200 fresh, priced three different ways.
    expected = (
        Decimal(200) * IN_RATE
        + Decimal(200) * CACHE_READ_RATE
        + Decimal(100) * CACHE_WRITE_RATE
        + Decimal(20) * OUT_RATE
    ).quantize(Decimal("0.000001"))
    assert Decimal(call.cost_usd) == expected
    # Provenance: the row must say these rates came from a gateway, so a $0
    # here could never be mistaken for a free call.
    assert call.pricing_source == "gateway"
    assert Decimal(call.input_rate_usd) == IN_RATE
    assert call.cache_write_tokens == 100


async def test_project_cost_rollup_includes_cache_costs(
    asgi_app, http_client, auth_bypass, monkeypatch
):
    """The number a human actually looks at must include the premium."""
    _gateway(monkeypatch, prompt_tokens=1000, cached=0, written=1000, completion=0)
    project_id = await _project(asgi_app, http_client)
    await _chat(asgi_app, project_id)

    resp = await http_client.get(f"/v1/projects/{project_id}/cost")
    assert resp.status_code == 200, resp.text
    total = Decimal(resp.json()["total_usd"])
    assert total == (Decimal(1000) * CACHE_WRITE_RATE).quantize(Decimal("0.000001")), (
        f"the cost rollup reports {total}, which does not include the "
        f"cache-write premium the ledger recorded."
    )
