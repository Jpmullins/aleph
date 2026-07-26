"""Per-call cost, from rates the gateway reports about itself.

This module used to carry a hand-maintained table of model names and rates,
with a docstring asserting it had been "verified against the Insights LiteLLM
Gateway". Measured against that very gateway, every entry was wrong: the names
were unprefixed (`claude-sonnet-4-6` vs `bedrock-claude-sonnet-4-6`, so every
call 400'd) and the rates were off by roughly 3x (Opus input priced at $15/MTok
against an actual $5.50). The table is gone rather than corrected — a
maintained-by-hand price list is wrong by default, and wrong prices that *look*
authoritative are worse than none.

Rates now come from :mod:`aleph_models.discovery`, which reads them from the
gateway's own `/model/info`.

**Unpriced is not free.** The previous implementation returned `$0` for any
model it did not recognise, which is how a completely broken pricing table
produced a plausible-looking $0.00 dashboard instead of an alarm. Callers now
receive :class:`CostBreakdown`, whose ``priced`` flag they must record, so an
uncosted call stays distinguishable from a genuinely free one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aleph_models.discovery import DiscoveredModel

_UNIT = Decimal("0.000001")


@dataclass(frozen=True)
class ModelPricing:
    input_per_token: Decimal
    output_per_token: Decimal
    #: Percentage off the input rate for cache *reads*. Used only when the
    #: gateway reports no absolute `cache_read_per_token`.
    cache_discount_pct: Decimal = Decimal("90")
    #: Multiplier on base input rate for tokens WRITTEN into the cache.
    #:
    #: Prompt caching is not free: a 5-minute cache write bills at 1.25x base
    #: input, and only subsequent *reads* get the discount. Modelling the
    #: discount without the premium made every first call in a cached
    #: conversation systematically under-report — the opposite failure to the
    #: one caching is meant to fix, and invisible because the reported number
    #: simply gets smaller. Used only when the gateway reports no write rate.
    cache_write_multiplier: Decimal = Decimal("1.25")
    #: Absolute rates as reported by the gateway. These take precedence: a
    #: derived percentage is an assumption, a reported rate is a fact.
    cache_read_per_token: Decimal | None = None
    cache_write_per_token: Decimal | None = None

    @property
    def read_rate(self) -> Decimal:
        if self.cache_read_per_token is not None:
            return self.cache_read_per_token
        return self.input_per_token * (Decimal("1") - self.cache_discount_pct / Decimal("100"))

    @property
    def write_rate(self) -> Decimal:
        if self.cache_write_per_token is not None:
            return self.cache_write_per_token
        return self.input_per_token * self.cache_write_multiplier


@dataclass(frozen=True)
class CostBreakdown:
    """What a call cost, and everything needed to re-derive it later."""

    cost_usd: Decimal
    cache_savings_usd: Decimal
    #: False when the model was absent from the table. The cost is then `0`
    #: because nothing better is available — but the caller must persist this
    #: fact so the zero is never mistaken for a free call.
    priced: bool
    #: `gateway` | `static` | `unknown` — provenance for the rates below.
    source: str = "unknown"
    #: The rates actually applied. Persisted alongside the call so a historical
    #: cost stays checkable after the gateway changes its prices.
    input_rate_usd: Decimal = Decimal("0")
    output_rate_usd: Decimal = Decimal("0")


class PricingTable:
    """Converts token counts to money. Empty until populated from a gateway."""

    def __init__(
        self, table: dict[str, ModelPricing] | None = None, *, source: str = "static"
    ) -> None:
        self._table: dict[str, ModelPricing] = dict(table) if table is not None else {}
        self._source = source

    @property
    def source(self) -> str:
        return self._source

    @classmethod
    def from_discovery(cls, models: list[DiscoveredModel]) -> PricingTable:
        """Build from `/model/info`, skipping models the gateway won't price."""
        table: dict[str, ModelPricing] = {}
        for m in models:
            if m.input_per_token is None:
                continue
            table[m.id] = ModelPricing(
                input_per_token=m.input_per_token,
                output_per_token=m.output_per_token or Decimal("0"),
                cache_read_per_token=m.cache_read_per_token,
                cache_write_per_token=m.cache_write_per_token,
                # A model reporting no cache rates has no cache pricing to
                # model; assuming Anthropic's 90/1.25 there would be a guess.
                cache_discount_pct=(
                    Decimal("90") if m.cache_read_per_token is not None else Decimal("0")
                ),
                cache_write_multiplier=(
                    Decimal("1.25") if m.cache_write_per_token is not None else Decimal("1")
                ),
            )
        return cls(table, source="gateway")

    def has(self, model: str) -> bool:
        return model in self._table

    def models(self) -> list[str]:
        return sorted(self._table)

    def merge(self, other: PricingTable) -> None:
        """Fold in another table, `other` winning. Used to refresh from a gateway."""
        self._table.update(other._table)
        if other._table:
            self._source = other._source

    def breakdown(
        self,
        *,
        model: str,
        input_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        cache_write_tokens: int = 0,
    ) -> CostBreakdown:
        """Cost, savings, and whether the model was priced at all.

        `cached_tokens` are cache **reads** (discounted). `cache_write_tokens`
        are tokens written into the cache on this call, billed at a premium.
        Both are subsets of `input_tokens`, so the un-cached remainder is
        `input_tokens - cached_tokens - cache_write_tokens`.
        """
        p = self._table.get(model)
        if p is None:
            return CostBreakdown(Decimal("0"), Decimal("0"), priced=False, source="unknown")

        full_input_tokens = max(input_tokens - cached_tokens - cache_write_tokens, 0)
        read_rate = p.read_rate
        cost = (
            Decimal(full_input_tokens) * p.input_per_token
            + Decimal(cached_tokens) * read_rate
            + Decimal(cache_write_tokens) * p.write_rate
            + Decimal(completion_tokens) * p.output_per_token
        )
        savings = Decimal(cached_tokens) * max(p.input_per_token - read_rate, Decimal("0"))
        return CostBreakdown(
            cost.quantize(_UNIT),
            savings.quantize(_UNIT),
            priced=True,
            source=self._source,
            input_rate_usd=p.input_per_token,
            output_rate_usd=p.output_per_token,
        )

    def cost_for(
        self,
        *,
        model: str,
        input_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        cache_write_tokens: int = 0,
    ) -> tuple[Decimal, Decimal]:
        """`(cost_usd, cache_savings_usd)`. Prefer :meth:`breakdown`.

        Kept for call sites that genuinely cannot act on `priced`; it discards
        exactly the signal that made unpriced models invisible, so anything
        persisting a cost should use :meth:`breakdown` instead.
        """
        b = self.breakdown(
            model=model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        return b.cost_usd, b.cache_savings_usd


def get_default_pricing() -> PricingTable:
    """An empty table.

    There is no default price list. Aleph learns rates from the gateway it is
    pointed at; until discovery runs it knows nothing, and says so rather than
    reporting zero.
    """
    return PricingTable()
