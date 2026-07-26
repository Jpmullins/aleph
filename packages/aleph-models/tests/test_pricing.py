"""Cost must be right, and when it cannot be right it must be loud.

This file used to assert the two behaviours that made Aleph's cost ledger
fiction against the first real gateway it met:

* `test_default_table_has_required_models` pinned a hand-written list of model
  names. Not one of them exists on the gateway.
* `test_unknown_model_returns_zero` pinned `$0` for unrecognised models *as the
  requirement*. Combined with the above, every call would have recorded zero —
  and the suite would have stayed green while the spend dashboard read $0.00
  through a live research run.

Both are now inverted. Rates come from the gateway; an unpriced model is a
flagged, visible condition rather than a free one.

The fixture is a verbatim capture of the Insights gateway's `/model/info`
(with `litellm_params` stripped), so the parsing under test faces the real wire
shape rather than one invented to suit it.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from aleph_models.discovery import parse_model_info
from aleph_models.pricing import ModelPricing, PricingTable, get_default_pricing

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "insights_model_info.json"


def _live_table() -> PricingTable:
    return PricingTable.from_discovery(parse_model_info(json.loads(FIXTURE.read_text())))


class TestNoGuessedPrices:
    def test_default_pricing_is_empty(self) -> None:
        """Aleph ships no price list.

        The previous default table asserted rates for models it had never
        contacted, and every one was wrong — Opus input at $15/MTok against an
        actual $5.50. An empty table cannot be wrong; it can only be
        unpopulated, which is a state the code reports.
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


class TestPricesComeFromTheGateway:
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
