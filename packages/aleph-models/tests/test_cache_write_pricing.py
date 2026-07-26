"""Prompt caching must not make costs *look* cheaper than they are.

The pricing table modelled the 90% cache-READ discount and nothing else. Cache
WRITES are billed at a premium (Anthropic: 1.25x base input for a 5-minute
cache), so turning caching on against the old table would have systematically
under-reported the cost of every first call in a cached conversation — while
`cache_savings_usd` grew. That is a worse failure than no caching: the number on
the dashboard moves in the reassuring direction for the wrong reason.

These tests pin the arithmetic and the parsing. They do not need a gateway; what
needs a live gateway is proving `cached_tokens` actually arrives, which is
tracked separately in END-STATE.md as E4.1.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aleph_models.client import _cache_write_tokens
from aleph_models.pricing import ModelPricing, PricingTable

MODEL = "m"
IN_RATE = Decimal("0.00001")
OUT_RATE = Decimal("0.00002")


def _table(*, discount: str = "90", write_mult: str = "1.25") -> PricingTable:
    return PricingTable(
        {
            MODEL: ModelPricing(
                input_per_token=IN_RATE,
                output_per_token=OUT_RATE,
                cache_discount_pct=Decimal(discount),
                cache_write_multiplier=Decimal(write_mult),
            )
        }
    )


class TestCacheWritePremium:
    def test_cache_write_costs_more_than_a_plain_input_token(self) -> None:
        """The whole point: a write is a premium, not a freebie."""
        t = _table()
        plain, _ = t.cost_for(model=MODEL, input_tokens=1000, cached_tokens=0, completion_tokens=0)
        written, _ = t.cost_for(
            model=MODEL,
            input_tokens=1000,
            cached_tokens=0,
            completion_tokens=0,
            cache_write_tokens=1000,
        )
        assert written > plain, (
            f"1000 cache-written tokens cost {written}, no more than the same "
            f"tokens uncached ({plain}) — the write premium is not applied and "
            f"every cache-priming call under-reports."
        )

    def test_exact_premium_arithmetic(self) -> None:
        t = _table()
        cost, _ = t.cost_for(
            model=MODEL,
            input_tokens=1000,
            cached_tokens=0,
            completion_tokens=0,
            cache_write_tokens=1000,
        )
        assert cost == (Decimal(1000) * IN_RATE * Decimal("1.25")).quantize(Decimal("0.000001"))

    def test_reads_writes_and_plain_input_are_billed_distinctly(self) -> None:
        """A realistic second turn: some cached, some written, some fresh."""
        t = _table()
        cost, savings = t.cost_for(
            model=MODEL,
            input_tokens=1000,
            cached_tokens=600,
            completion_tokens=0,
            cache_write_tokens=200,
        )
        expected = (
            Decimal(200) * IN_RATE  # 1000 - 600 - 200 uncached
            + Decimal(600) * IN_RATE * Decimal("0.1")  # reads at 10%
            + Decimal(200) * IN_RATE * Decimal("1.25")  # writes at 125%
        ).quantize(Decimal("0.000001"))
        assert cost == expected
        assert savings == (Decimal(600) * IN_RATE * Decimal("0.9")).quantize(Decimal("0.000001"))

    def test_no_double_counting_of_the_written_portion(self) -> None:
        """Writes are a subset of input; billing them twice would over-report."""
        t = _table()
        cost, _ = t.cost_for(
            model=MODEL,
            input_tokens=1000,
            cached_tokens=0,
            completion_tokens=0,
            cache_write_tokens=1000,
        )
        naive_double = (Decimal(1000) * IN_RATE) + (Decimal(1000) * IN_RATE * Decimal("1.25"))
        assert cost < naive_double.quantize(Decimal("0.000001"))

    def test_non_caching_models_carry_no_premium(self) -> None:
        """Embedding/open-weight models must not be silently marked up."""
        t = _table(discount="0", write_mult="1")
        cost, _ = t.cost_for(
            model=MODEL,
            input_tokens=1000,
            cached_tokens=0,
            completion_tokens=0,
            cache_write_tokens=1000,
        )
        assert cost == (Decimal(1000) * IN_RATE).quantize(Decimal("0.000001"))

    def test_default_is_backwards_compatible(self) -> None:
        """Existing call sites that pass no write tokens are unchanged."""
        t = _table()
        a, sa = t.cost_for(model=MODEL, input_tokens=500, cached_tokens=100, completion_tokens=7)
        b, sb = t.cost_for(
            model=MODEL,
            input_tokens=500,
            cached_tokens=100,
            completion_tokens=7,
            cache_write_tokens=0,
        )
        assert (a, sa) == (b, sb)

    def test_discovered_table_prices_writes_for_caching_models_only(self) -> None:
        """Discovery must not invent a cache premium, nor drop a real one.

        Aleph no longer ships a price list — rates come from the gateway. So
        the invariant moves to the mapping: a model the gateway gives cache
        rates for must carry a premium, and one it does not must carry none.
        Marking up a model with no cache pricing would over-report every call.
        """
        from aleph_models.discovery import parse_model_info
        from aleph_models.pricing import PricingTable

        models = parse_model_info(
            {
                "data": [
                    {
                        "model_name": "caching-model",
                        "model_info": {
                            "mode": "chat",
                            "input_cost_per_token": 5.5e-06,
                            "output_cost_per_token": 2.75e-05,
                            "cache_read_input_token_cost": 5.5e-07,
                            "cache_creation_input_token_cost": 6.875e-06,
                        },
                    },
                    {
                        "model_name": "plain-model",
                        "model_info": {
                            "mode": "embedding",
                            "input_cost_per_token": 2e-07,
                            "output_cost_per_token": 0.0,
                        },
                    },
                ]
            }
        )
        table = PricingTable.from_discovery(models)

        caching = table.breakdown(
            model="caching-model",
            input_tokens=1000,
            cached_tokens=0,
            completion_tokens=0,
            cache_write_tokens=1000,
        )
        plain_input = Decimal(1000) * Decimal("5.5e-06")
        assert caching.cost_usd > plain_input.quantize(Decimal("0.000001")), (
            "gateway reported a cache-creation rate but the write was billed at "
            "or below base input — the premium was dropped in translation."
        )

        no_cache = table.breakdown(
            model="plain-model",
            input_tokens=1000,
            cached_tokens=0,
            completion_tokens=0,
            cache_write_tokens=1000,
        )
        assert no_cache.cost_usd == (Decimal(1000) * Decimal("2e-07")).quantize(
            Decimal("0.000001")
        ), "a model the gateway gives no cache rates for was marked up anyway"


class TestUsageParsing:
    """An unrecognised key means the write is billed as a free uncached token."""

    @pytest.mark.parametrize(
        "key",
        ["cache_creation_input_tokens", "cache_write_tokens", "cached_write_tokens"],
    )
    def test_top_level_spellings(self, key: str) -> None:
        assert _cache_write_tokens({key: 321}, {}) == 321

    @pytest.mark.parametrize("key", ["cache_creation_input_tokens", "cache_write_tokens"])
    def test_nested_in_prompt_tokens_details(self, key: str) -> None:
        assert _cache_write_tokens({}, {key: 77}) == 77

    def test_absent_means_zero_not_an_error(self) -> None:
        assert _cache_write_tokens({}, {}) == 0
        assert _cache_write_tokens({"prompt_tokens": 10}, None) == 0

    def test_top_level_wins_over_nested(self) -> None:
        assert (
            _cache_write_tokens({"cache_creation_input_tokens": 5}, {"cache_write_tokens": 9}) == 5
        )
