"""Cost must be right, and when it cannot be right it must be loud.

This file used to assert the two behaviours that made Aleph's cost ledger
fiction against the first real gateway it met:

* `test_default_table_has_required_models` pinned a hand-written list of model
  names. Those names were right for the gateway they were written against and
  wrong for the next one — which is the property a committed table cannot fix.
* `test_unknown_model_returns_zero` pinned `$0` for unrecognised models *as the
  requirement*. That is the load-bearing defect: on a gateway whose names did
  not match, every call recorded zero, and the suite would have stayed green
  while the spend dashboard read $0.00 through a live research run.

Both are now inverted. Rates are learned at runtime; an unpriced model is a
flagged, visible condition rather than a free one.

The fixture is a verbatim `/model/info` capture from a Bedrock-backed LiteLLM
gateway (with `litellm_params` stripped), so the parsing under test faces a real
wire shape rather than one invented to suit it.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from aleph_models.discovery import DiscoveredModel, parse_model_info
from aleph_models.pricing import ModelPricing, PricingTable, get_default_pricing

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "bedrock_gateway_model_info.json"


def _live_table() -> PricingTable:
    return PricingTable.from_discovery(parse_model_info(json.loads(FIXTURE.read_text())))


class TestNoGuessedPrices:
    def test_default_pricing_is_empty(self) -> None:
        """Aleph ships no price list.

        The previous default table asserted rates as fact for gateways it had
        never contacted. On one of them Opus input was priced at $15/MTok
        against an actual $5.50. An empty table cannot be wrong about a
        deployment; it can only be unpopulated, which is a state the code
        reports rather than papers over.
        """
        assert get_default_pricing().models() == []

    def test_unknown_model_is_flagged_not_free(self) -> None:
        """THE regression. A model we cannot price must not cost $0 silently."""
        b = get_default_pricing().breakdown(
            model="totally-fake", input_tokens=100_000, cached_tokens=0, completion_tokens=5_000
        )
        assert b.priced is False, (
            "an unpriceable call reported itself as priced — this is exactly how "
            "a pricing table matching none of the gateway's model names produced "
            "a $0.00 ledger that read as a quiet day"
        )
        assert b.source == "unknown"
        assert b.cost_usd == Decimal("0")


class TestPricesComeFromTheGatewayThatReportsThem:
    def test_every_advertised_model_is_priced(self) -> None:
        table = _live_table()
        models = parse_model_info(json.loads(FIXTURE.read_text()))
        assert models, "fixture parsed to nothing"
        for m in models:
            assert table.has(m.id), f"gateway advertises {m.id} but discovery did not price it"

    def test_rates_match_the_gateway_exactly(self) -> None:
        """No rounding, no float artifacts, no assumed list price."""
        b = _live_table().breakdown(
            model="bedrock-claude-opus-4.7",
            input_tokens=1_000_000,
            cached_tokens=0,
            completion_tokens=0,
        )
        # Gateway reports 5.5e-06/token = $5.50 per million.
        assert b.input_rate_usd == Decimal("5.5E-6")
        assert b.cost_usd == Decimal("5.500000")
        assert b.source == "gateway"

    def test_cache_read_uses_the_reported_rate_not_an_assumed_discount(self) -> None:
        """A reported rate is a fact; a 90% rule of thumb is an assumption."""
        b = _live_table().breakdown(
            model="bedrock-claude-opus-4.7",
            input_tokens=100_000,
            cached_tokens=100_000,
            completion_tokens=0,
        )
        # 100k reads at the gateway's 5.5e-07 = $0.055.
        assert b.cost_usd == Decimal("0.055000")
        # Savings measured against the base rate actually charged.
        assert b.cache_savings_usd == Decimal("0.495000")

    def test_embedding_model_carries_no_invented_cache_premium(self) -> None:
        b = _live_table().breakdown(
            model="bedrock-titan-embed-text",
            input_tokens=1_000_000,
            cached_tokens=0,
            completion_tokens=0,
            cache_write_tokens=0,
        )
        assert b.cost_usd == Decimal("0.200000")


class TestArithmetic:
    def test_percentage_tables_still_work(self) -> None:
        """Operator-supplied tables without absolute rates keep working."""
        table = PricingTable(
            {
                "m": ModelPricing(
                    input_per_token=Decimal("0.000003"),
                    output_per_token=Decimal("0.000015"),
                    cache_discount_pct=Decimal("90"),
                )
            }
        )
        b = table.breakdown(model="m", input_tokens=1000, cached_tokens=200, completion_tokens=100)
        # (800 * 3e-6) + (200 * 3e-6 * 0.1) + (100 * 15e-6)
        assert b.cost_usd == Decimal("0.003960")
        assert b.cache_savings_usd == Decimal("0.000540")
        assert b.source == "static"

    @pytest.mark.parametrize("cached", [0, 500, 1000])
    def test_cost_never_exceeds_the_all_uncached_price(self, cached: int) -> None:
        """Caching may only reduce cost. A mis-signed premium would surface here."""
        table = _live_table()
        full = table.breakdown(
            model="bedrock-claude-opus-4.6",
            input_tokens=1000,
            cached_tokens=0,
            completion_tokens=0,
        )
        got = table.breakdown(
            model="bedrock-claude-opus-4.6",
            input_tokens=1000,
            cached_tokens=cached,
            completion_tokens=0,
        )
        assert got.cost_usd <= full.cost_usd


class TestProvenanceIsRecordable:
    def test_breakdown_carries_the_rates_used(self) -> None:
        """Without these, a row cannot explain itself once prices change."""
        b = _live_table().breakdown(
            model="bedrock-claude-opus-4.6",
            input_tokens=10,
            cached_tokens=0,
            completion_tokens=10,
        )
        assert b.input_rate_usd > 0
        assert b.output_rate_usd > 0
        recomputed = (Decimal(10) * b.input_rate_usd + Decimal(10) * b.output_rate_usd).quantize(
            Decimal("0.000001")
        )
        assert b.cost_usd == recomputed, (
            "cost cannot be re-derived from the persisted rates — the audit trail "
            "would not survive the gateway changing its prices"
        )


# ---------------------------------------------------------------------------
# Per-model provenance
#
# The table carried ONE `_source` for everything in it, and `from_discovery`
# labelled the whole table `static` if any single priced model came from hints.
# On a normal restricted-key gateway — where most rates are reported and a few
# are filled in — that meant every cost row claimed to be asserted. "Asserted"
# and "reported" carry very different weight in an audit.
# ---------------------------------------------------------------------------


def _discovered(model_id: str, *, rates_source: str) -> DiscoveredModel:
    return DiscoveredModel(
        id=model_id,
        mode="chat",
        input_per_token=Decimal("0.000003"),
        output_per_token=Decimal("0.000015"),
        max_input_tokens=200_000,
        max_output_tokens=8_192,
        cache_read_per_token=None,
        cache_write_per_token=None,
        supports_vision=False,
        supports_function_calling=True,
        supports_reasoning=False,
        supports_prompt_caching=False,
        rates_source=rates_source,
    )


def test_two_models_in_one_table_keep_different_provenance() -> None:
    table = PricingTable.from_discovery(
        [
            _discovered("reported-model", rates_source="gateway"),
            _discovered("asserted-model", rates_source="static"),
        ]
    )

    reported = table.breakdown(
        model="reported-model", input_tokens=100, cached_tokens=0, completion_tokens=10
    )
    asserted = table.breakdown(
        model="asserted-model", input_tokens=100, cached_tokens=0, completion_tokens=10
    )

    assert reported.source == "gateway", (
        "a gateway-reported rate was relabelled because ANOTHER model in the "
        "same table came from hints"
    )
    assert asserted.source == "static"
    # Both are genuinely priced — the labels differ, the arithmetic does not.
    assert reported.priced and asserted.priced
    assert reported.cost_usd == asserted.cost_usd


def test_merging_a_hints_table_does_not_relabel_gateway_rows() -> None:
    """A refresh must not downgrade the provenance of rates it did not touch."""
    live = PricingTable.from_discovery([_discovered("reported-model", rates_source="gateway")])
    hints = PricingTable.from_discovery([_discovered("asserted-model", rates_source="static")])

    live.merge(hints)

    assert (
        live.breakdown(
            model="reported-model", input_tokens=10, cached_tokens=0, completion_tokens=1
        ).source
        == "gateway"
    )
    assert (
        live.breakdown(
            model="asserted-model", input_tokens=10, cached_tokens=0, completion_tokens=1
        ).source
        == "static"
    )


def test_an_absent_model_is_unknown_and_unpriced() -> None:
    """The check has to be able to say "no" — otherwise `source` is a constant."""
    table = PricingTable.from_discovery([_discovered("known", rates_source="gateway")])
    out = table.breakdown(model="never-seen", input_tokens=10, cached_tokens=0, completion_tokens=1)
    assert out.priced is False
    assert out.source == "unknown"
    assert out.cost_usd == Decimal("0")


def test_merge_is_in_place_so_every_holder_sees_the_refresh() -> None:
    """The kernel hands ONE PricingTable to LiteLLMClient and to the agent's cost
    callback. A merge that returned a new table would strand both holders on the
    old one — which is how the agent path spent its life with an empty table."""
    live = PricingTable()
    holder = live  # what a long-lived consumer captured at boot

    assert not holder.has("late-arriving-model")
    live.merge(
        PricingTable.from_discovery([_discovered("late-arriving-model", rates_source="gateway")])
    )
    assert holder.has("late-arriving-model"), "the refresh did not reach an existing holder"
    assert (
        holder.breakdown(
            model="late-arriving-model", input_tokens=10, cached_tokens=0, completion_tokens=1
        ).source
        == "gateway"
    )


def test_a_client_built_with_an_empty_pricing_table_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty table is legal and silent, and its consequence is not.

    Every `ModelCall` such a client writes lands `pricing_source="unknown"`,
    `cost_usd=0` — "this call was free" — in an append-only ledger, and those
    rows are what `status_numbers.py` counts as uncosted. The retrieval eval
    passed `PricingTable()` and wrote 90 of them before anything noticed, and
    what noticed was a health number two days later.

    Not raised, because `copilot_cost_callback` legitimately falls back to an
    empty table before the kernel binds one. Warned, so a caller who MEANT to
    pass rates finds out at construction.
    """
    import logging
    from typing import Any, cast

    import httpx
    import structlog

    from aleph_models.client import LiteLLMClient

    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    try:
        with caplog.at_level(logging.WARNING):
            LiteLLMClient(
                base_url="http://gateway.invalid",
                api_key="k",
                http_client=httpx.AsyncClient(),
                pricing=PricingTable(),
                session_maker=cast("Any", object()),
            )
        assert "litellm.pricing_table_empty" in caplog.text, (
            f"no warning for an empty pricing table; logged: {caplog.text!r}"
        )
    finally:
        structlog.reset_defaults()


def test_a_client_built_with_real_rates_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Otherwise the warning fires on every well-configured client and is noise.

    Without this, hardcoding the warning unconditionally would pass the test
    above.
    """
    import logging
    from typing import Any, cast

    import httpx
    import structlog

    from aleph_models.client import LiteLLMClient
    from aleph_models.discovery import DiscoveredModel

    priced = PricingTable.from_discovery(
        [
            DiscoveredModel(
                id="a-model",
                mode="chat",
                max_input_tokens=128_000,
                max_output_tokens=8_192,
                input_per_token=Decimal("0.000001"),
                output_per_token=Decimal("0.000002"),
                cache_read_per_token=None,
                cache_write_per_token=None,
                supports_vision=False,
                supports_function_calling=True,
                supports_reasoning=False,
                supports_prompt_caching=False,
            )
        ]
    )
    assert priced.models(), "the fixture itself has no rates, so this proves nothing"

    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    try:
        with caplog.at_level(logging.WARNING):
            LiteLLMClient(
                base_url="http://gateway.invalid",
                api_key="k",
                http_client=httpx.AsyncClient(),
                pricing=priced,
                session_maker=cast("Any", object()),
            )
        assert "litellm.pricing_table_empty" not in caplog.text
    finally:
        structlog.reset_defaults()
