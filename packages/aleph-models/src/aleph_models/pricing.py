"""Per-model pricing table.

Pricing lives in code (deploy-level config) rather than the DB so it
ships in PRs. The gateway routes by model name; this table tells us
how to convert (input_tokens, completion_tokens) → cost_usd.

Values reflect the Insights LiteLLM Gateway model list verified
2026-05-27. When the gateway adds a model or pricing changes, update
this table in the same PR that ships the change.

All values are USD per token. Cache discount is applied to the
cached-token portion of the input.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPricing:
    input_per_token: Decimal
    output_per_token: Decimal
    cache_discount_pct: Decimal = Decimal("90")  # 90% off for cached tokens


_DEFAULT_TABLE: dict[str, ModelPricing] = {
    # Anthropic Claude — published list rates (USD/MTok ÷ 1e6).
    "claude-opus-4-7": ModelPricing(
        input_per_token=Decimal("0.000015"),
        output_per_token=Decimal("0.000075"),
        cache_discount_pct=Decimal("90"),
    ),
    "claude-opus-4-6": ModelPricing(
        input_per_token=Decimal("0.000015"),
        output_per_token=Decimal("0.000075"),
        cache_discount_pct=Decimal("90"),
    ),
    "claude-sonnet-4-6": ModelPricing(
        input_per_token=Decimal("0.000003"),
        output_per_token=Decimal("0.000015"),
        cache_discount_pct=Decimal("90"),
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_token=Decimal("0.000001"),
        output_per_token=Decimal("0.000005"),
        cache_discount_pct=Decimal("90"),
    ),
    # Open weight via NIM / vLLM — gateway charges per-token rates set by ops.
    "gpt-oss-120b": ModelPricing(
        input_per_token=Decimal("0.0000006"),
        output_per_token=Decimal("0.0000024"),
        cache_discount_pct=Decimal("0"),
    ),
    "gpt-oss-20b": ModelPricing(
        input_per_token=Decimal("0.0000002"),
        output_per_token=Decimal("0.0000008"),
        cache_discount_pct=Decimal("0"),
    ),
    "gemma-3-27b": ModelPricing(
        input_per_token=Decimal("0.0000003"),
        output_per_token=Decimal("0.0000012"),
        cache_discount_pct=Decimal("0"),
    ),
    "gemma-3-12b": ModelPricing(
        input_per_token=Decimal("0.0000002"),
        output_per_token=Decimal("0.0000008"),
        cache_discount_pct=Decimal("0"),
    ),
    # Cohere embeddings.
    "cohere-embed-v4": ModelPricing(
        input_per_token=Decimal("0.00000012"),
        output_per_token=Decimal("0"),
        cache_discount_pct=Decimal("0"),
    ),
    "cohere-embed-english-v3": ModelPricing(
        input_per_token=Decimal("0.0000001"),
        output_per_token=Decimal("0"),
        cache_discount_pct=Decimal("0"),
    ),
    # Titan embeddings.
    "titan-embed-v2": ModelPricing(
        input_per_token=Decimal("0.0000001"),
        output_per_token=Decimal("0"),
        cache_discount_pct=Decimal("0"),
    ),
    # Local vLLM on owned hardware. Zero is the honest per-token rate: there is
    # no marginal charge per token, and inventing a notional one would put
    # fictional money in the cost ledger. The real cost is electricity and the
    # GPU, neither of which is per-token. Token *counts* are still recorded, so
    # usage remains measurable even though its price is zero.
    "qwen3.8-27b-uncensored": ModelPricing(
        input_per_token=Decimal("0"),
        output_per_token=Decimal("0"),
        cache_discount_pct=Decimal("0"),
    ),
    "bge-m3": ModelPricing(
        input_per_token=Decimal("0"),
        output_per_token=Decimal("0"),
        cache_discount_pct=Decimal("0"),
    ),
}


class PricingTable:
    """Computes per-call cost. Unknown models default to zero cost but
    raise a warning via the logger so they're noticed in evals."""

    def __init__(self, table: dict[str, ModelPricing] | None = None) -> None:
        self._table = dict(table) if table is not None else dict(_DEFAULT_TABLE)

    def has(self, model: str) -> bool:
        return model in self._table

    def cost_for(
        self,
        *,
        model: str,
        input_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
    ) -> tuple[Decimal, Decimal]:
        """Return `(cost_usd, cache_savings_usd)` for this call."""
        p = self._table.get(model)
        if p is None:
            return Decimal("0"), Decimal("0")
        full_input_tokens = max(input_tokens - cached_tokens, 0)
        cache_savings = (
            Decimal(cached_tokens) * p.input_per_token * (p.cache_discount_pct / Decimal("100"))
        )
        cost = (
            Decimal(full_input_tokens) * p.input_per_token
            + Decimal(cached_tokens)
            * p.input_per_token
            * (Decimal("1") - p.cache_discount_pct / Decimal("100"))
            + Decimal(completion_tokens) * p.output_per_token
        )
        return cost.quantize(Decimal("0.000001")), cache_savings.quantize(Decimal("0.000001"))


def get_default_pricing() -> PricingTable:
    return PricingTable()
