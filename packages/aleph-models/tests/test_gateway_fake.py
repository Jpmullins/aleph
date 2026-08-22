"""The fake gateway must actually misbehave, and must misbehave by default.

A shared test double is only worth having if it reproduces the failures the
real thing produces. These tests are the fake's own acceptance: each one names
a defect Aleph has shipped or survived, and asserts the fake can produce it.

The load-bearing one is `TestTheDefaultIsHostile`. A fake that is permissive
unless configured otherwise silently weakens every test built on it — each
would be written against a gateway kinder than production, pass, and prove
nothing about the deployment it claims to describe. So the default is a
restricted virtual key with no published rates, and that is pinned here rather
than left as a convention someone can "simplify" away.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from aleph_core.errors import ValidationFailed
from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage, LiteLLMClient
from aleph_models.discovery import discover_models, probe_model, select_default_bindings
from aleph_models.pricing import PricingTable
from aleph_models.testing import (
    FakeGateway,
    FakeModel,
    GatewayConfig,
    RecordingSessions,
    rate_limited,
)
from aleph_security.principal import Principal

CHAT = "/v1/chat/completions"
EMBED = "/v1/embeddings"

#: The name the deployed profile bound. No gateway serves it — the gateway
#: serves `titan-embed-text-v2` — and every embed 400'd, which is why
#: `document_chunks` has 0 rows against 75 sources.
WRONG_EMBEDDER = "titan-embed-v2"


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(), subject="test", email="t@example.com", actor_kind="aleph_agent"
    )


def _litellm(
    fake: FakeGateway,
    http: httpx.AsyncClient,
    sessions: RecordingSessions,
    *,
    pricing: PricingTable | None = None,
) -> LiteLLMClient:
    """A real `LiteLLMClient` — retry policy, cost path and all — on the fake."""
    return LiteLLMClient(
        base_url=fake.base_url,
        api_key=fake.api_key,
        http_client=http,
        pricing=pricing or PricingTable(),
        session_maker=cast("Any", sessions),
    )


# ---------------------------------------------------------------------------


class TestTheDefaultIsHostile:
    """The guard on the whole design. Do not relax these.

    If `FakeGateway()` ever starts answering `/model/info`, every test written
    since becomes a test of a gateway that does not exist in production.
    """

    def test_default_config_refuses_model_info_and_publishes_no_rates(self) -> None:
        cfg = GatewayConfig()
        assert cfg.model_info_allowed is False
        assert cfg.report_rates is False

    async def test_a_bare_fake_403s_the_admin_route(self) -> None:
        fake = FakeGateway()
        async with fake.client() as http:
            resp = await http.get(
                "/model/info", headers={"Authorization": f"Bearer {fake.api_key}"}
            )
        assert resp.status_code == 403
        assert "llm_api_routes" in resp.text

    async def test_allowing_model_info_alone_still_reports_no_rates(self) -> None:
        """Two independent hostilities, not one switch.

        Unlocking the admin route must not also hand over prices — an operator
        key that can read `/model/info` on a gateway whose config sets no costs
        is a real deployment, and it is the one where an unpriced call has to
        stay visible.
        """
        fake = FakeGateway(GatewayConfig(model_info_allowed=True))
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
        assert models
        assert all(m.rates_source != "gateway" for m in models)

    async def test_asking_for_a_cooperative_gateway_is_explicit(self) -> None:
        """`well_behaved` is the only way to get the friendly answers."""
        fake = FakeGateway(GatewayConfig.well_behaved())
        async with fake.client() as http:
            resp = await http.get(
                "/model/info", headers={"Authorization": f"Bearer {fake.api_key}"}
            )
        assert resp.status_code == 200


class TestModelInfoForbiddenFallback:
    """The restricted-virtual-key case, which is the *normal* one."""

    @pytest.mark.parametrize("status", [401, 403])
    async def test_discovery_falls_back_to_v1_models(self, status: int) -> None:
        fake = FakeGateway(GatewayConfig(model_info_status=status))
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )

        assert fake.count("/model/info") == 1, "the good route must be tried first"
        assert fake.count("/v1/models") == 1, "and the fallback must actually be taken"
        assert [m.id for m in models] == [
            "bedrock-claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-opus-4-7",
            "titan-embed-text-v2",
        ], "a restricted key produced no models; Settings would render empty"

    async def test_the_fallback_carries_ids_and_nothing_else(self) -> None:
        """`metadata_available=False` is what stops an id being auto-bound.

        `/v1/models` says nothing about mode, window, tools or price. Anything
        the fake volunteered here would let a test pass on facts the real
        fallback cannot supply.
        """
        fake = FakeGateway(GatewayConfig(models=(FakeModel(id="some-local-model"),)))
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
        (only,) = models
        assert only.metadata_available is False
        assert only.mode is None
        assert only.max_input_tokens is None
        assert only.is_priced is False
        assert only.rates_source == "none"


class TestRatesPresentVersusAbsent:
    async def test_rates_absent_are_never_labelled_as_reported(self) -> None:
        """Silence from the gateway must not become a `gateway` price.

        Note what discovery legitimately does here: `aleph_models.hints` fills
        rates the gateway did not report and labels them `hints`. That is the
        real path and the fake must not hide it — the guarantee being pinned is
        narrower and more important. An *asserted* rate is never presented as a
        *reported* one, because that distinction is the only thing separating a
        number an auditor can trust from one somebody typed.
        """
        fake = FakeGateway(GatewayConfig.well_behaved(report_rates=False))
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
        assert models
        assert all(m.rates_source != "gateway" for m in models)

    async def test_a_model_nothing_prices_stays_unpriced_not_free(self) -> None:
        """The defect this reproduces: an unknown model cost `$0` silently, so
        a ledger over a live research run read $0.00 and looked like a quiet
        day. `vllm-local-*` is in no hints file, which is correct — a
        self-hosted model's cost is a property of somebody's hardware.
        """
        fake = FakeGateway(GatewayConfig.well_behaved(models=(FakeModel(id="vllm-local-mixtral"),)))
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
        (only,) = models
        assert only.is_priced is False
        assert only.rates_source == "none"
        assert PricingTable.from_discovery(models).models() == []

    async def test_rates_present_survive_as_exact_decimals(self) -> None:
        fake = FakeGateway(GatewayConfig.well_behaved())
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
        opus = next(m for m in models if m.id == "claude-opus-4-7")
        assert opus.input_per_token == Decimal("5.5E-6")
        assert opus.cache_write_per_token == Decimal("6.875E-6")
        assert opus.rates_source == "gateway"
        # The embedder is deliberately unpriced even here: on the reference
        # gateway the only reachable embedder carries no published rate.
        embed = next(m for m in models if m.id == "titan-embed-text-v2")
        assert embed.is_priced is False


class TestRateLimiting:
    async def test_429_carries_retry_after_when_scripted_to(self) -> None:
        fake = FakeGateway(GatewayConfig(invoke_script=(rate_limited(retry_after="7"),)))
        async with fake.client() as http:
            resp = await http.post(
                CHAT,
                headers={"Authorization": f"Bearer {fake.api_key}"},
                json={"model": "claude-haiku-4-5", "messages": []},
            )
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "7"

    async def test_429_without_retry_after_omits_the_header_entirely(self) -> None:
        """The header is optional. Code that assumes it sleeps zero and hammers.

        `None` must mean *absent*, not `"0"` — a caller reading `"0"` cannot
        tell "no guidance" from "retry immediately".
        """
        fake = FakeGateway(GatewayConfig(invoke_script=(rate_limited(retry_after=None),)))
        async with fake.client() as http:
            resp = await http.post(
                CHAT,
                headers={"Authorization": f"Bearer {fake.api_key}"},
                json={"model": "claude-haiku-4-5", "messages": []},
            )
        assert resp.status_code == 429
        assert "retry-after" not in resp.headers

    async def test_the_script_is_consumed_in_order_then_normal_service_resumes(self) -> None:
        fake = FakeGateway(GatewayConfig(invoke_script=(rate_limited(times=2),)))
        headers = {"Authorization": f"Bearer {fake.api_key}"}
        body = {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]}
        async with fake.client() as http:
            first = await http.post(CHAT, headers=headers, json=body)
            second = await http.post(CHAT, headers=headers, json=body)
            third = await http.post(CHAT, headers=headers, json=body)
        assert [first.status_code, second.status_code, third.status_code] == [429, 429, 200]

    async def test_the_client_retry_policy_actually_retries_a_429(self) -> None:
        """Proves the 429 reaches the real retry policy, not just the socket.

        A 429 that succeeds on retry is the only shape that distinguishes "we
        retried" from "the call worked first time". Costs one real backoff
        second; that is the price of exercising `gateway_retry` rather than a
        re-implementation of it.
        """
        fake = FakeGateway(GatewayConfig(invoke_script=(rate_limited(),)))
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = _litellm(fake, http, sessions)
            resp = await client.chat(
                principal=_principal(),
                project_id=uuid4(),
                agent_run_id=None,
                capability=Capability.CLASSIFICATION,
                profile_bindings={"classification": {"model": "claude-haiku-4-5"}},
                messages=[ChatMessage(role="user", content="hello there")],
                purpose="test.retry",
            )
        assert fake.count(CHAT) == 2, "the 429 was not retried"
        assert resp.choices[0].message.content == "pong"
        assert len(sessions.model_calls()) == 1, "the successful call must still be costed"


class TestListsButFailsOnInvocation:
    """A gateway's model list states configuration, not reachability."""

    async def test_probe_returns_the_gateways_own_words(self) -> None:
        fake = FakeGateway(GatewayConfig.well_behaved())
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
            broken = next(m for m in models if m.id == "bedrock-claude-sonnet-4-6")
            error = await probe_model(
                base_url=fake.base_url, api_key=fake.api_key, model=broken, client=http
            )
        assert error is not None
        assert "inference profile" in error, (
            "the operator must see what the gateway said, not 'something went wrong'"
        )

    async def test_the_unreachable_model_is_never_bound_by_default(self) -> None:
        """It lists as a priced, tool-capable Sonnet — a plausible default."""
        fake = FakeGateway(GatewayConfig.well_behaved())
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
        bound_blind = select_default_bindings(models)
        bound_probed = select_default_bindings(
            models, unreachable=frozenset({"bedrock-claude-sonnet-4-6"})
        )
        chosen = {b["model"] for b in bound_probed.values()}
        assert "bedrock-claude-sonnet-4-6" not in chosen
        assert bound_blind != bound_probed, (
            "if probing changes nothing, the fixture cannot detect an unprobed binding"
        )


class TestEmbeddingMode:
    async def test_embeddings_return_the_configured_width(self) -> None:
        fake = FakeGateway()
        async with fake.client() as http:
            resp = await http.post(
                EMBED,
                headers={"Authorization": f"Bearer {fake.api_key}"},
                json={"model": "titan-embed-text-v2", "input": ["a", "b"]},
            )
        body = resp.json()
        assert resp.status_code == 200
        assert len(body["data"]) == 2
        assert len(body["data"][0]["embedding"]) == 1024, (
            "1024 is document_chunks.embedding's width; a different one is a mismatch"
        )

    async def test_a_narrower_embedder_is_expressible(self) -> None:
        """The dimension-mismatch case, which costs a write path its dense leg."""
        fake = FakeGateway(
            GatewayConfig(models=(FakeModel(id="small", mode="embedding", embedding_dim=256),))
        )
        async with fake.client() as http:
            resp = await http.post(
                EMBED,
                headers={"Authorization": f"Bearer {fake.api_key}"},
                json={"model": "small", "input": ["a"]},
            )
        assert len(resp.json()["data"][0]["embedding"]) == 256

    async def test_a_chat_model_posted_to_the_embeddings_route_400s(self) -> None:
        """Posting to the wrong route must fail, or `probe_model`'s route
        choice is untested and an embedder can be probed as a chat model."""
        fake = FakeGateway()
        headers = {"Authorization": f"Bearer {fake.api_key}"}
        async with fake.client() as http:
            wrong_route = await http.post(
                EMBED, headers=headers, json={"model": "claude-haiku-4-5", "input": ["a"]}
            )
            also_wrong = await http.post(
                CHAT, headers=headers, json={"model": "titan-embed-text-v2", "messages": []}
            )
        assert wrong_route.status_code == 400
        assert also_wrong.status_code == 400

    async def test_a_model_name_the_gateway_does_not_serve_400s(self) -> None:
        """The dead-RAG defect, end to end through the real client.

        The profile bound `titan-embed-v2`; the gateway serves
        `titan-embed-text-v2`. One wrong name, every embed 400s, and because
        chunks are only written after the embed returns it also killed the
        lexical leg — which needs no model at all.
        """
        fake = FakeGateway()
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = _litellm(fake, http, sessions)
            with pytest.raises(httpx.HTTPStatusError) as raised:
                await client.embed(
                    principal=_principal(),
                    project_id=uuid4(),
                    agent_run_id=None,
                    profile_bindings={"embedding": {"model": WRONG_EMBEDDER}},
                    input=["anything"],
                    purpose="test.dead_rag",
                )
        assert raised.value.response.status_code == 400
        assert WRONG_EMBEDDER in raised.value.response.text, (
            "the refusal must name the model, or an operator cannot tell a typo from an outage"
        )
        assert fake.models_requested() == [WRONG_EMBEDDER], (
            "the assertion has to name the model that went out, not just that it failed"
        )
        assert sessions.model_calls() == [], "a failed call must not be costed as a success"

    async def test_an_unserved_name_is_never_quietly_substituted(self) -> None:
        """The mutation this test exists to kill.

        A fake that answered any unknown name with whichever model it happens
        to serve first would let every wrong-model-name test above pass for the
        wrong reason — on the embeddings route the substituted chat model still
        400s on mode, so the mode check masks it entirely. On the chat route a
        substitution succeeds, which is where it becomes visible. Silently
        serving a name nobody asked for is also the one behaviour that would
        make this fake unable to reproduce the defect it was built for.
        """
        fake = FakeGateway()
        async with fake.client() as http:
            resp = await http.post(
                CHAT,
                headers={"Authorization": f"Bearer {fake.api_key}"},
                json={
                    "model": "claude-opus-4-7-typo",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 400
        assert "claude-opus-4-7-typo" in resp.json()["error"]["message"]


class TestSlowResponses:
    async def test_a_stalled_gateway_takes_at_least_its_configured_latency(self) -> None:
        fake = FakeGateway(GatewayConfig(latency_s=0.05))
        started = time.monotonic()
        async with fake.client() as http:
            await http.get("/v1/models", headers={"Authorization": f"Bearer {fake.api_key}"})
        assert time.monotonic() - started >= 0.05

    async def test_a_caller_can_time_the_stall_out(self) -> None:
        """`asyncio.timeout`, deliberately — not httpx's `timeout=`.

        `httpx.ASGITransport` does not implement transport timeouts, so
        `timeout=0.01` on an in-process fake is silently ignored. Anything
        testing timeout HANDLING must bound the await itself; a test that
        passes `timeout=` here would report a pass while measuring nothing.
        """
        fake = FakeGateway(GatewayConfig(latency_s=5.0))
        async with fake.client() as http:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await http.get(
                        "/v1/models", headers={"Authorization": f"Bearer {fake.api_key}"}
                    )


class TestRequestCounting:
    async def test_every_request_is_counted_per_path(self) -> None:
        fake = FakeGateway()
        headers = {"Authorization": f"Bearer {fake.api_key}"}
        async with fake.client() as http:
            await http.get("/model/info", headers=headers)
            await http.get("/v1/models", headers=headers)
            await http.get("/v1/models", headers=headers)
        assert fake.request_count == 3
        assert fake.count("/v1/models") == 2
        assert fake.count("/model/info") == 1
        assert fake.count(CHAT) == 0

    async def test_two_fakes_count_independently(self) -> None:
        """The shape MEP-4's isolation criterion needs: B moved, A did not."""
        a, b = FakeGateway(), FakeGateway()
        async with b.client() as http:
            await http.get("/v1/models", headers={"Authorization": f"Bearer {b.api_key}"})
        assert b.request_count == 1
        assert a.request_count == 0

    async def test_reset_clears_counters_and_reloads_the_script(self) -> None:
        fake = FakeGateway(GatewayConfig(invoke_script=(rate_limited(),)))
        headers = {"Authorization": f"Bearer {fake.api_key}"}
        body = {"model": "claude-haiku-4-5", "messages": []}
        async with fake.client() as http:
            assert (await http.post(CHAT, headers=headers, json=body)).status_code == 429
            fake.reset()
            assert fake.request_count == 0
            assert (await http.post(CHAT, headers=headers, json=body)).status_code == 429

    async def test_peak_concurrency_is_recorded(self) -> None:
        """What MEP-2's ceiling test reads. Without the stall every request
        finishes before the next starts and the peak is a constant 1."""
        fake = FakeGateway(GatewayConfig(latency_s=0.05))
        headers = {"Authorization": f"Bearer {fake.api_key}"}
        async with fake.client() as http:
            await asyncio.gather(*(http.get("/v1/models", headers=headers) for _ in range(8)))
        assert fake.peak_in_flight == 8
        assert fake.request_count == 8

    async def test_serialised_requests_peak_at_one(self) -> None:
        """The recorder must be able to say 'not concurrent', or it is a max()
        that only ever counts total requests."""
        fake = FakeGateway(GatewayConfig(latency_s=0.01))
        headers = {"Authorization": f"Bearer {fake.api_key}"}
        async with fake.client() as http:
            for _ in range(5):
                await http.get("/v1/models", headers=headers)
        assert fake.peak_in_flight == 1


class TestAuth:
    async def test_a_wrong_bearer_token_is_401(self) -> None:
        fake = FakeGateway()
        async with fake.client() as http:
            resp = await http.get("/v1/models", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    async def test_the_bearer_token_that_went_out_is_recorded(self) -> None:
        fake = FakeGateway()
        async with fake.client() as http:
            await discover_models(base_url=fake.base_url, api_key=fake.api_key, client=http)
        assert {r.authorization for r in fake.requests} == {f"Bearer {fake.api_key}"}

    async def test_an_unknown_route_answers_json_not_plaintext(self) -> None:
        """A gateway 404s as JSON; code that only saw Starlette's plaintext 404
        reports 'expecting value' instead of 'no such route'."""
        fake = FakeGateway()
        async with fake.client() as http:
            resp = await http.get(
                "/model_group/info", headers={"Authorization": f"Bearer {fake.api_key}"}
            )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "404"


class TestTheCostPathRunsForReal:
    async def test_a_priced_call_records_a_gateway_sourced_model_call(self) -> None:
        """Not a fake-gateway property as such — a check that the fake is rich
        enough to drive the real cost path, which is what MEP-1 measures."""
        fake = FakeGateway(GatewayConfig.well_behaved())
        sessions = RecordingSessions()
        async with fake.client() as http:
            models = await discover_models(
                base_url=fake.base_url, api_key=fake.api_key, client=http
            )
            client = _litellm(fake, http, sessions, pricing=PricingTable.from_discovery(models))
            await client.chat(
                principal=_principal(),
                project_id=uuid4(),
                agent_run_id=None,
                capability=Capability.CLASSIFICATION,
                profile_bindings={"classification": {"model": "claude-haiku-4-5"}},
                messages=[ChatMessage(role="user", content="one two three four")],
                purpose="test.cost",
            )
        (call,) = sessions.model_calls()
        assert call.model == "claude-haiku-4-5"
        assert call.pricing_source == "gateway"
        assert call.input_tokens == 4, "token counts must vary with input, or cost math is untested"
        assert call.cost_usd > Decimal("0")

    async def test_an_unpriced_call_is_recorded_as_unknown_not_zero(self) -> None:
        fake = FakeGateway()  # hostile default: nothing is priced
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = _litellm(fake, http, sessions)
            await client.chat(
                principal=_principal(),
                project_id=uuid4(),
                agent_run_id=None,
                capability=Capability.CLASSIFICATION,
                profile_bindings={"classification": {"model": "claude-haiku-4-5"}},
                messages=[ChatMessage(role="user", content="hi")],
                purpose="test.unpriced",
            )
        (call,) = sessions.model_calls()
        assert call.pricing_source == "unknown"

    async def test_an_unbound_capability_never_reaches_the_gateway(self) -> None:
        fake = FakeGateway()
        sessions = RecordingSessions()
        async with fake.client() as http:
            client = _litellm(fake, http, sessions)
            with pytest.raises(ValidationFailed):
                await client.chat(
                    principal=_principal(),
                    project_id=uuid4(),
                    agent_run_id=None,
                    capability=Capability.VISION,
                    profile_bindings={},
                    messages=[ChatMessage(role="user", content="hi")],
                    purpose="test.unbound",
                )
        assert fake.request_count == 0
