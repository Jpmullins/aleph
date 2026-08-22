"""Gateway traffic is countable — by purpose, by outcome, and by how it was priced.

WS-P9 criteria 2 and 3, pinned where the number is produced rather than where it
is rendered. Three questions Aleph could not answer before, each now one query:

  * "how many gateway calls did that turn make, and for what" — backlog E5's
    'weirdly rate limited' is a request-rate question and nothing counted
    requests;
  * "is the gateway failing, or is nothing calling it" — indistinguishable on a
    success-only counter, and they need opposite responses;
  * "what share of our spend is unpriced" — backlog E4, previously answerable
    only by counting rows in a database by hand.

The gateway is `aleph_models.testing.FakeGateway`, whose defaults are
deliberately hostile: a restricted virtual key that publishes no rates. That is
what makes the `pricing_source="unknown"` assertion below a real one — it is the
deployment shape that produced 159 uncosted `model_calls` rows.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage, LiteLLMClient
from aleph_models.discovery import discover_models
from aleph_models.pricing import PricingTable
from aleph_models.testing import (
    FakeGateway,
    GatewayConfig,
    RecordingSessions,
    server_error,
)
from aleph_observability import metrics as m
from aleph_security.principal import Principal

HAIKU = "claude-haiku-4-5"


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(), subject="test", email="t@example.com", actor_kind="aleph_agent"
    )


def _count(name: str, **labels: str) -> float:
    value = m.sample_value(name, **labels)
    return 0.0 if value is None else value


def _client(
    fake: FakeGateway,
    http: httpx.AsyncClient,
    sessions: RecordingSessions,
    *,
    pricing: PricingTable | None = None,
) -> LiteLLMClient:
    return LiteLLMClient(
        base_url=fake.base_url,
        api_key=fake.api_key,
        http_client=http,
        pricing=pricing or PricingTable(),
        session_maker=cast("Any", sessions),
        retry_sleep=_no_sleep,
    )


async def _no_sleep(_seconds: float) -> None:
    """Retries are real; waiting for them is not what this file tests."""


async def _chat(client: LiteLLMClient, purpose: str) -> None:
    await client.chat(
        principal=_principal(),
        project_id=uuid4(),
        agent_run_id=None,
        capability=Capability.CLASSIFICATION,
        profile_bindings={"classification": {"model": HAIKU}},
        messages=[ChatMessage(role="user", content="one two three four")],
        purpose=purpose,
    )


# ---------------------------------------------------------------------------


async def test_two_purposes_produce_two_series() -> None:
    """Criterion 2. Per-purpose request rate is the number E5 needs."""
    fake = FakeGateway(GatewayConfig.well_behaved())
    sessions = RecordingSessions()
    labels_a = {"capability": "classification", "purpose": "test.p9.alpha", "outcome": "ok"}
    labels_b = {"capability": "classification", "purpose": "test.p9.beta", "outcome": "ok"}
    before_a = _count(m.LLM_REQUESTS, **labels_a)
    before_b = _count(m.LLM_REQUESTS, **labels_b)

    async with fake.client() as http:
        client = _client(fake, http, sessions)
        await _chat(client, "test.p9.alpha")
        await _chat(client, "test.p9.beta")
        await _chat(client, "test.p9.beta")

    assert _count(m.LLM_REQUESTS, **labels_a) == before_a + 1
    assert _count(m.LLM_REQUESTS, **labels_b) == before_b + 2, (
        "the two purposes did not separate; a fan-out hypothesis cannot be "
        "confirmed or dropped from a single undifferentiated total"
    )


async def test_a_dead_gateway_moves_the_failure_counter_and_stops_the_success_one() -> None:
    """The whole review step of WS-P9, in one process.

    "A metric that does not move when the thing it measures breaks is
    decoration." The gateway is scripted to 503 every attempt, so the call
    exhausts its retries and raises. The error series must rise by exactly one
    (one logical call, however many HTTP attempts) and the ok series must not
    move at all.
    """
    fake = FakeGateway(
        GatewayConfig.well_behaved(invoke_script=(server_error(status=503, times=99),))
    )
    sessions = RecordingSessions()
    labels = {"capability": "classification", "purpose": "test.p9.outage"}
    ok_before = _count(m.LLM_REQUESTS, **labels, outcome="ok")
    err_before = _count(m.LLM_REQUESTS, **labels, outcome="error")

    async with fake.client() as http:
        client = _client(fake, http, sessions)
        with pytest.raises(Exception):  # noqa: B017 — the transport error type is not the point
            await _chat(client, "test.p9.outage")

    assert _count(m.LLM_REQUESTS, **labels, outcome="error") == err_before + 1, (
        "the gateway refused every attempt and the failure counter did not "
        "move — this metric would be flat during an outage"
    )
    assert _count(m.LLM_REQUESTS, **labels, outcome="ok") == ok_before, (
        "a failed call incremented the success counter"
    )


async def test_an_unpriced_call_is_attributed_to_pricing_source_unknown() -> None:
    """Criterion 3, on the hostile default: a gateway that publishes no rates."""
    fake = FakeGateway()  # restricted key, no rates — the shipped shape
    sessions = RecordingSessions()
    base = {
        "capability": "classification",
        "purpose": "test.p9.unpriced",
        "pricing_source": "unknown",
    }
    before = _count(m.LLM_TOKENS, **base, kind="input")

    async with fake.client() as http:
        await _chat(_client(fake, http, sessions), "test.p9.unpriced")

    (call,) = sessions.model_calls()
    assert call.pricing_source == "unknown"
    assert _count(m.LLM_TOKENS, **base, kind="input") == before + call.input_tokens, (
        "unpriced tokens were not attributed to pricing_source=unknown, so the "
        "unpriced share of spend is still an anecdote"
    )
    assert m.sample_value(m.LLM_COST, **base) is not None, (
        f"no {m.LLM_COST} series for an unpriced call — an unpriced call must "
        "be a visible $0, not an absent one"
    )


async def test_a_priced_call_is_attributed_to_the_gateway() -> None:
    """The other half of the same label: `unknown` only means something if
    `gateway` is also produced by the same path."""
    fake = FakeGateway(GatewayConfig.well_behaved())
    sessions = RecordingSessions()
    async with fake.client() as http:
        models = await discover_models(base_url=fake.base_url, api_key=fake.api_key, client=http)
        client = _client(fake, http, sessions, pricing=PricingTable.from_discovery(models))
        await _chat(client, "test.p9.priced")

    (call,) = sessions.model_calls()
    assert call.pricing_source == "gateway"
    cost = m.sample_value(
        m.LLM_COST,
        capability="classification",
        purpose="test.p9.priced",
        pricing_source="gateway",
    )
    assert cost is not None and cost > 0, f"gateway-priced spend was not counted: {cost}"


async def test_embedding_failures_are_counted_too() -> None:
    """The embedder is the leg whose silent death emptied the retrieval index.

    `titan-embed-v2` against a gateway serving `titan-embed-text-v2` produced 45
    index runs stuck in `running`, 0 chunks, and no signal anywhere (WS-RS1).
    A per-outcome counter on the embed path is what would have said so.
    """
    fake = FakeGateway(GatewayConfig.well_behaved())
    sessions = RecordingSessions()
    labels = {"capability": "embedding", "purpose": "test.p9.embed_miss"}
    before = _count(m.LLM_REQUESTS, **labels, outcome="error")

    async with fake.client() as http:
        client = _client(fake, http, sessions)
        with pytest.raises(Exception):  # noqa: B017 — a 400 from the gateway
            await client.embed(
                principal=_principal(),
                project_id=uuid4(),
                agent_run_id=None,
                profile_bindings={"embedding": {"model": "titan-embed-v2"}},
                input=["anything"],
                purpose="test.p9.embed_miss",
            )

    assert _count(m.LLM_REQUESTS, **labels, outcome="error") == before + 1
