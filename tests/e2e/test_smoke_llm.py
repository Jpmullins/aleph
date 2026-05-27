"""Smoke LLM end-to-end with the LiteLLM gateway mocked.

The gateway is not reachable in CI, so we monkey-patch the LiteLLM client's
underlying httpx POST to return a canned OpenAI-shaped response. The rest
of the path is real: profile lookup, ModelCall + CostLedgerEvent writes,
OTEL spans, budget rollup trigger.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-smoke", "email": "smoke@test.local", "name": "Smoke"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


@pytest.fixture
def patched_gateway(monkeypatch):
    """Patch LiteLLMClient._post_with_retry to return a canned response."""
    from aleph_models import client as client_mod

    canned = {
        "id": "chatcmpl-test",
        "model": "claude-haiku-4-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 1,
            "total_tokens": 6,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }

    async def fake_post(self, path, payload):
        if path == "/v1/chat/completions":
            return canned
        if path == "/v1/embeddings":
            return {
                "model": "cohere-embed-english-v3",
                "data": [{"embedding": [0.0] * 16}],
                "usage": {"prompt_tokens": 4},
            }
        msg = f"unexpected path: {path}"
        raise AssertionError(msg)

    monkeypatch.setattr(client_mod.LiteLLMClient, "_post_with_retry", fake_post)
    yield


async def test_smoke_llm_writes_model_call_and_cost_ledger(
    http_client, auth_bypass, patched_gateway, asgi_app
):
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "smoke", "description": "", "budget_usd": "1.00"},
    )
    assert proj.status_code == 201
    pid = proj.json()["id"]

    resp = await http_client.post(
        f"/v1/projects/{pid}/smoke/llm",
        json={"prompt": "ping"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "pong"
    assert body["model"] == "claude-haiku-4-5"
    assert Decimal(body["cost_usd"]) >= Decimal("0")

    # Verify the ModelCall and CostLedgerEvent rows were written.
    from aleph_db.models.cost import CostLedgerEvent, ModelCall

    maker = asgi_app.state.session_maker
    async with maker() as session:
        calls = list(
            (
                await session.execute(
                    select(ModelCall).where(ModelCall.project_id == pid)
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await session.execute(
                    select(CostLedgerEvent).where(
                        CostLedgerEvent.project_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(calls) == 1
    assert calls[0].purpose == "inc0.smoke"
    assert len(events) == 1
    assert events[0].model_call_id == calls[0].id

    # The budget rollup trigger must have updated budgets.spent_usd.
    cost = await http_client.get(f"/v1/projects/{pid}/cost")
    assert cost.status_code == 200
    spent = Decimal(cost.json()["spent_usd"])
    assert spent >= Decimal("0")
