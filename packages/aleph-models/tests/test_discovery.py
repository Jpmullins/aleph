"""Discovery must describe the gateway, and defaults must be usable.

Aleph binds capabilities to whatever the gateway serves. Two ways that goes
wrong, and the second is the quiet one:

* **Binding nothing** — a capability is unbound, resolution raises, and the
  failure is immediate and legible.
* **Binding something that cannot do the job** — the cheapest chat model on
  the real gateway has an 8k context and no function calling. Selected for
  `page_selection`, which loads the whole wiki index into one prompt, it fails
  only under load, on real content, after the run has already cost money.

So most of what follows is about *refusal*: refusing unpriced models, refusing
models that miss a hard requirement, refusing models the gateway advertises but
cannot actually reach.

The fixture is a verbatim `/model/info` capture from the Insights gateway. The
selection logic under test therefore faces the real world's awkward shape — two
Opuses at identical prices, two Sonnets that 4xx on invocation, one tiny cheap
model, one embedder — rather than a tidy invented one.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from typing import Any

import httpx
import pytest

from aleph_core.schemas.model_profile import Capability
from aleph_models.discovery import (
    CAPABILITY_POLICIES,
    DiscoveredModel,
    candidates_for,
    discover_models,
    parse_model_info,
    probe_model,
    select_default_bindings,
    unbound_capabilities,
    unpriced_bindings,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "bedrock_gateway_model_info.json"

#: Verified by probing the live gateway: both advertise fine and both fail on
#: invocation (one "Access denied", one missing an inference profile).
UNREACHABLE = frozenset({"bedrock-claude-sonnet-4", "bedrock-claude-sonnet-4-6"})


def _models() -> list[DiscoveredModel]:
    return parse_model_info(json.loads(FIXTURE.read_text()))


def _by_id(models: list[DiscoveredModel]) -> dict[str, DiscoveredModel]:
    return {m.id: m for m in models}


class TestParsing:
    def test_parses_the_real_gateway_payload(self) -> None:
        models = _models()
        assert [m.id for m in models] == [
            "bedrock-claude-opus-4.6",
            "bedrock-claude-opus-4.7",
            "bedrock-claude-sonnet-4",
            "bedrock-claude-sonnet-4-6",
            "bedrock-llama3-70b",
            "bedrock-titan-embed-text",
        ]

    def test_rates_survive_as_exact_decimals(self) -> None:
        """Money must not inherit binary float error."""
        m = _by_id(_models())["bedrock-claude-opus-4.7"]
        assert m.input_per_token == Decimal("5.5E-6")
        assert m.cache_read_per_token == Decimal("5.5E-7")
        assert m.cache_write_per_token == Decimal("6.875E-6")

    def test_capability_flags_and_modes_are_read(self) -> None:
        by_id = _by_id(_models())
        opus = by_id["bedrock-claude-opus-4.7"]
        assert (opus.mode, opus.supports_vision, opus.supports_function_calling) == (
            "chat",
            True,
            True,
        )
        embed = by_id["bedrock-titan-embed-text"]
        assert embed.mode == "embedding"
        llama = by_id["bedrock-llama3-70b"]
        assert llama.supports_function_calling is False
        assert llama.max_input_tokens == 8192

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"data": []},
            {"data": [{"no_model_name": 1}]},
            {"data": ["not-a-dict"]},
            {"data": [{"model_name": "", "model_info": {}}]},
            [],
            "garbage",
            None,
        ],
    )
    def test_malformed_payloads_do_not_explode(self, payload: Any) -> None:
        assert parse_model_info(payload) == []

    def test_one_bad_row_does_not_lose_the_good_ones(self) -> None:
        models = parse_model_info(
            {"data": ["junk", {"model_name": "ok", "model_info": {"mode": "chat"}}]}
        )
        assert [m.id for m in models] == ["ok"]

    def test_unpriced_model_is_marked_not_defaulted_to_zero(self) -> None:
        m = parse_model_info({"data": [{"model_name": "x", "model_info": {"mode": "chat"}}]})[0]
        assert m.input_per_token is None
        assert m.is_priced is False


class TestTransport:
    @pytest.mark.asyncio
    async def test_discovery_hits_model_info_with_a_bearer_token(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=json.loads(FIXTURE.read_text()))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            models = await discover_models(
                base_url="https://gw.example/", api_key="sekret", client=c
            )
        assert seen["url"] == "https://gw.example/model/info"
        assert seen["auth"] == "Bearer sekret"
        assert len(models) == 6

    @pytest.mark.asyncio
    async def test_probe_reports_the_gateway_error_rather_than_raising(self) -> None:
        """A broken model must be a value we can route around, not an exception."""
        body = {"error": {"message": "BedrockException - on-demand throughput isn't supported"}}

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(400, json=body))
        ) as c:
            err = await probe_model(
                base_url="https://gw.example",
                api_key="k",
                model=_by_id(_models())["bedrock-claude-sonnet-4-6"],
                client=c,
            )
        assert err is not None
        assert "throughput" in err

    @pytest.mark.asyncio
    async def test_probe_returns_none_when_the_model_works(self) -> None:
        ok = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}]}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=ok))
        ) as c:
            assert (
                await probe_model(
                    base_url="https://gw.example",
                    api_key="k",
                    model=_by_id(_models())["bedrock-claude-opus-4.7"],
                    client=c,
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_probe_uses_the_embeddings_route_for_embedders(self) -> None:
        """Posting chat messages to an embedder would report a false failure."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"data": [{"embedding": [0.0]}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await probe_model(
                base_url="https://gw.example",
                api_key="k",
                model=_by_id(_models())["bedrock-titan-embed-text"],
                client=c,
            )
        assert seen["path"] == "/v1/embeddings"


class TestSelectionRefuses:
    def test_unpriced_models_are_ranked_last_and_flagged(self) -> None:
        """Unpriced is a last resort that announces itself, not a disqualifier.

        Excluding unpriced models outright was the first design, and it was too
        strict in a way that mattered: on the reference gateway the only
        *reachable* embedder carries no published rate, so exclusion left
        `embedding` unbindable and the platform unusable. Binding it records
        `pricing_source="unknown"` — honest but useless for cost control — so
        the rule is: never prefer an unpriced model over a priced one, and
        always report the ones chosen anyway.
        """
        priced_and_unpriced = parse_model_info(
            {
                "data": [
                    {
                        "model_name": "priced",
                        "model_info": {
                            "mode": "chat",
                            "max_input_tokens": 1_000_000,
                            "supports_function_calling": True,
                            "input_cost_per_token": 1e-06,
                            "output_cost_per_token": 2e-06,
                        },
                    },
                    {
                        "model_name": "unpriced-but-mighty",
                        "model_info": {
                            "mode": "chat",
                            "max_input_tokens": 1_000_000,
                            "supports_function_calling": True,
                            "supports_reasoning": True,
                        },
                    },
                ]
            }
        )
        bindings = select_default_bindings(priced_and_unpriced)
        assert bindings[Capability.SYNTHESIS.value]["model"] == "priced", (
            "an unpriced model outranked a priced one — every call on it would "
            "record pricing_source=unknown"
        )
        assert unpriced_bindings(bindings, priced_and_unpriced) == []

    def test_unpriced_is_still_reported_when_it_is_the_only_option(self) -> None:
        free_lunch = parse_model_info(
            {
                "data": [
                    {
                        "model_name": "unpriced-but-mighty",
                        "model_info": {
                            "mode": "chat",
                            "max_input_tokens": 1_000_000,
                            "supports_function_calling": True,
                            "supports_reasoning": True,
                        },
                    }
                ]
            }
        )
        bindings = select_default_bindings(free_lunch)
        assert bindings, "refusing every option leaves the platform unusable"
        flagged = unpriced_bindings(bindings, free_lunch)
        assert Capability.SYNTHESIS.value in flagged, (
            "an unpriced binding was made silently — the whole point is that a "
            "$0 cost is never mistaken for a free call"
        )

    def test_page_selection_refuses_a_small_context_model(self) -> None:
        """The trap: cheapest-wins would pick an 8k model for a whole-index prompt."""
        models = [m for m in _models() if m.id not in UNREACHABLE]
        chosen = select_default_bindings(models)[Capability.PAGE_SELECTION.value]["model"]
        assert chosen != "bedrock-llama3-70b", (
            "page_selection bound the cheapest chat model, which has an 8k "
            "context — it would fail once the wiki index outgrew it, not before"
        )

    def test_extraction_refuses_a_model_without_function_calling(self) -> None:
        models = [m for m in _models() if m.id not in UNREACHABLE]
        chosen = select_default_bindings(models)[Capability.EXTRACTION.value]["model"]
        assert _by_id(_models())[chosen].supports_function_calling

    def test_vision_refuses_a_model_without_vision(self) -> None:
        for cand in candidates_for(CAPABILITY_POLICIES[Capability.VISION], _models()):
            assert cand.supports_vision

    def test_embedding_never_binds_a_chat_model(self) -> None:
        chosen = select_default_bindings(_models())[Capability.EMBEDDING.value]["model"]
        assert _by_id(_models())[chosen].mode == "embedding"

    def test_capability_with_no_qualifying_model_is_left_unbound(self) -> None:
        """Better an honest error at resolution than a wrong model at runtime."""
        bindings = select_default_bindings(_models())
        assert Capability.RERANK.value not in bindings
        assert unbound_capabilities(bindings) == [Capability.RERANK]

    def test_unreachable_models_are_excluded(self) -> None:
        """Advertised is not the same as reachable — both Sonnets 4xx on call."""
        bindings = select_default_bindings(_models(), unreachable=UNREACHABLE)
        bound = {b["model"] for b in bindings.values()} | {
            b["fallback"]["model"] for b in bindings.values() if "fallback" in b
        }
        assert not (bound & UNREACHABLE), f"bound an unreachable model: {bound & UNREACHABLE}"

    def test_excluding_everything_yields_no_bindings_rather_than_a_guess(self) -> None:
        assert (
            select_default_bindings(_models(), unreachable=frozenset(m.id for m in _models())) == {}
        )


class TestSelectionChooses:
    def test_every_other_capability_is_bound_on_the_real_gateway(self) -> None:
        bindings = select_default_bindings(_models(), unreachable=UNREACHABLE)
        missing = [c.value for c in Capability if c.value not in bindings]
        assert missing == ["rerank"], f"unexpectedly unbound: {missing}"

    def test_heavy_capabilities_prefer_the_newer_model_when_tied(self) -> None:
        """Both Opuses are identical on price, context and flags — take the later."""
        bindings = select_default_bindings(_models(), unreachable=UNREACHABLE)
        assert bindings[Capability.SYNTHESIS.value]["model"] == "bedrock-claude-opus-4.7"

    def test_bindings_carry_the_gateway_rates_and_context_window(self) -> None:
        """Otherwise a binding silently reintroduces the guessed 200k/zero-cost defaults."""
        b = select_default_bindings(_models(), unreachable=UNREACHABLE)[Capability.SYNTHESIS.value]
        assert b["max_input_tokens"] == 1_000_000
        assert b["cost_per_input_token_usd"] == Decimal("5.5E-6")
        assert b["cost_per_output_token_usd"] == Decimal("2.75E-5")

    def test_fallback_is_a_different_qualifying_model(self) -> None:
        b = select_default_bindings(_models(), unreachable=UNREACHABLE)[Capability.SYNTHESIS.value]
        assert b["fallback"]["model"] != b["model"]
        assert b["fallback"]["model"] not in UNREACHABLE

    def test_single_candidate_gets_no_fallback_rather_than_a_wrong_one(self) -> None:
        b = select_default_bindings(_models())[Capability.EMBEDDING.value]
        assert "fallback" not in b


class TestDeterminism:
    def test_same_gateway_yields_identical_defaults(self) -> None:
        """Defaults are config; they must not drift between two boots."""
        a = select_default_bindings(_models(), unreachable=UNREACHABLE)
        b = select_default_bindings(_models(), unreachable=UNREACHABLE)
        assert a == b

    def test_independent_of_the_order_the_gateway_lists_models(self) -> None:
        forward = select_default_bindings(_models(), unreachable=UNREACHABLE)
        reverse = select_default_bindings(list(reversed(_models())), unreachable=UNREACHABLE)
        assert forward == reverse


def test_every_capability_has_a_policy() -> None:
    """A capability with no policy can never be bound, silently."""
    assert set(CAPABILITY_POLICIES) == set(Capability)


class TestRestrictedKeyFallback:
    """A virtual key cannot call `/model/info`; discovery must not need it.

    LiteLLM gates that route behind admin scope, so the *normal* case for an
    application is a 403. Treating it as fatal would have made discovery work
    only for whoever holds the admin key — and the reference deployment answers
    exactly that way.
    """

    @staticmethod
    def _routing_client(model_info_status: int) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/model/info":
                return httpx.Response(
                    model_info_status,
                    json={
                        "detail": "Virtual key is not allowed to call this route. "
                        "Only allowed to call routes: ['llm_api_routes']"
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "claude-haiku-4-5", "object": "model", "owned_by": "openai"},
                        {"id": "titan-embed-v2", "object": "model", "owned_by": "openai"},
                        {"id": "some-local-model", "object": "model", "owned_by": "openai"},
                    ]
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_falls_back_to_v1_models(self, status: int) -> None:
        async with self._routing_client(status) as c:
            models = await discover_models(base_url="https://gw.example", api_key="k", client=c)
        assert [m.id for m in models] == [
            "claude-haiku-4-5",
            "some-local-model",
            "titan-embed-v2",
        ], "a restricted key produced no models at all; Settings would be empty"

    @pytest.mark.asyncio
    async def test_hints_make_fallback_models_bindable(self) -> None:
        """Ids alone cannot be filtered by mode or window; hints supply that."""
        async with self._routing_client(403) as c:
            models = await discover_models(base_url="https://gw.example", api_key="k", client=c)
        by_id = _by_id(models)
        assert by_id["claude-haiku-4-5"].mode == "chat"
        assert by_id["titan-embed-v2"].mode == "embedding"

        bindings = select_default_bindings(models)
        assert bindings[Capability.EMBEDDING.value]["model"] == "titan-embed-v2"
        assert bindings[Capability.CLASSIFICATION.value]["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_a_model_no_hint_covers_stays_unbindable(self) -> None:
        """Silence is the correct answer for a model nothing describes."""
        async with self._routing_client(403) as c:
            models = await discover_models(base_url="https://gw.example", api_key="k", client=c)
        unknown = _by_id(models)["some-local-model"]
        assert unknown.mode is None
        assert unknown.metadata_available is False
        assert unknown.is_priced is False
        assert unknown.id not in {b["model"] for b in select_default_bindings(models).values()}


class TestRateProvenance:
    """An asserted rate must never be recorded as a reported one."""

    def test_gateway_reported_rates_are_labelled_gateway(self) -> None:
        from aleph_models.pricing import PricingTable

        table = PricingTable.from_discovery(_models())
        assert table.source == "gateway"

    @pytest.mark.asyncio
    async def test_hint_supplied_rates_are_labelled_static(self) -> None:
        """The bug this caught: a hint that set `mode` also flipped the label.

        `metadata_available` and rate provenance are different questions.
        Conflating them reported publisher list prices as if the gateway had
        quoted them — false confidence in the one number that must not carry it.
        """
        from aleph_models.pricing import PricingTable

        async with TestRestrictedKeyFallback._routing_client(403) as c:
            models = await discover_models(base_url="https://gw.example", api_key="k", client=c)
        haiku = _by_id(models)["claude-haiku-4-5"]
        assert haiku.is_priced, "hints did not price a model they cover"
        assert haiku.rates_source == "hints"
        assert PricingTable.from_discovery(models).source == "static"

    def test_hints_never_override_the_gateway(self) -> None:
        """A deployment with an admin key must not be downgraded by this file."""
        from aleph_models.hints import apply_hints

        reported = parse_model_info(
            {
                "data": [
                    {
                        "model_name": "claude-haiku-4-5",
                        "model_info": {
                            "mode": "chat",
                            "max_input_tokens": 999,
                            "input_cost_per_token": 1.23e-09,
                            "output_cost_per_token": 4.56e-09,
                        },
                    }
                ]
            }
        )
        after = apply_hints(reported)[0]
        assert after.input_per_token == Decimal("1.23E-9")
        assert after.max_input_tokens == 999
        assert after.rates_source == "gateway"
