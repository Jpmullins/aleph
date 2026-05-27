"""Pricing table tests."""

from __future__ import annotations

from decimal import Decimal

from aleph_models.pricing import PricingTable, get_default_pricing


def test_default_table_has_required_models() -> None:
    p = get_default_pricing()
    for m in (
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "cohere-embed-v4",
        "cohere-embed-english-v3",
    ):
        assert p.has(m), m


def test_unknown_model_returns_zero() -> None:
    p = get_default_pricing()
    cost, savings = p.cost_for(
        model="totally-fake", input_tokens=10, cached_tokens=0, completion_tokens=10
    )
    assert cost == Decimal("0")
    assert savings == Decimal("0")


def test_cost_with_cache_discount() -> None:
    table = PricingTable()
    # 1000 input, 200 cached (=90% off), 100 completion on claude-sonnet-4-6.
    cost, savings = table.cost_for(
        model="claude-sonnet-4-6",
        input_tokens=1000,
        cached_tokens=200,
        completion_tokens=100,
    )
    # input portion: (800 * 3e-6) + (200 * 3e-6 * 0.1) = 0.0024 + 0.00006 = 0.00246
    # output portion: 100 * 15e-6 = 0.0015
    # total: 0.00396
    assert cost == Decimal("0.003960")
    # savings: 200 * 3e-6 * 0.9 = 0.00054
    assert savings == Decimal("0.000540")
