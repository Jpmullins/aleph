"""Per-call cost, from rates learned at runtime rather than committed.

This module used to carry a hand-maintained table of model names and rates.
Against the primary gateway its **names were right** — `claude-opus-4-7`,
`claude-sonnet-4-6`, `claude-haiku-4-5` and the embedders all exist there. The
problems were the ones a committed table always has:

* **It is gateway-specific and nothing said so.** Pointed at a second, Bedrock-
  backed gateway, not one name matched (`claude-sonnet-4-6` vs
  `bedrock-claude-sonnet-4-6`), so every call 400'd — and the reported rates
  were roughly 3x that gateway's actual billing.
* **Its rates were unverifiable.** They were publisher list prices presented as
  "verified against the gateway". A gateway may mark up, discount, or host a
  model whose cost depends entirely on someone's hardware.
* **And an unknown model cost `$0`, silently.** This is the defect that made
  the others invisible: on the mismatched gateway the ledger would have read
  $0.00 across a live research run, which looks like a quiet day rather than a
  broken pipeline. A test pinned that behaviour *as the requirement*.

Rates now come from :mod:`aleph_models.discovery`, which prefers the gateway's
own `/model/info`. Where a virtual key may not call that admin route, the
operator hints file supplies what it can, and the difference is recorded:
``pricing_source`` is ``gateway`` for reported rates and ``static`` for asserted
ones.

**Unpriced is not free.** Callers receive :class:`CostBreakdown`, whose
``priced`` flag they must record, so an uncosted call stays distinguishable from
a genuinely free one.
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
    #: Where THIS model's rates came from — `gateway` (the endpoint reported
    #: them) or `static` (asserted from the operator hints file).
    #:
    #: Per model, not per table. The table carried one `_source` for everything
    #: in it and `from_discovery` labelled the whole thing `static` if any single
    #: priced model came from hints — so on a normal restricted-key gateway,
    #: where most rates are reported and a few are filled in, every cost row
    #: claimed to be asserted. "Asserted" and "reported" carry very different
    #: weight in an audit, and collapsing them loses the distinction in the
    #: direction that understates confidence in the numbers that deserve it.
    source: str = "static"

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
        """Build from discovery, skipping models nothing could price.

        `source` reflects where the numbers came from: `gateway` when the
        gateway reported its own rates, `static` when they were filled in from
        the operator hints file. The distinction is persisted on every
        `ModelCall`, because an asserted rate and a reported one carry very
        different weight in an audit.
        """
        table: dict[str, ModelPricing] = {}
        for m in models:
            if m.input_per_token is None:
                continue
            table[m.id] = ModelPricing(
                source=m.rates_source or "static",
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
        # The table-level label survives only as the fallback for a model that
        # is not in the table at all. Per-model provenance is on each row, so a
        # gateway-reported rate is no longer relabelled `static` because some
        # OTHER model in the same table came from hints.
        priced = [m for m in models if m.input_per_token is not None]
        reported = bool(priced) and all(m.rates_source == "gateway" for m in priced)
        return cls(table, source="gateway" if reported else "static")

    def has(self, model: str) -> bool:
        return model in self._table

    def models(self) -> list[str]:
        return sorted(self._table)

    def merge(self, other: PricingTable) -> None:
        """Fold in another table, `other` winning. Used to refresh from a gateway.

        Merging in place is deliberate and load-bearing: the kernel's PRICING
        capability hands the SAME object to `LiteLLMClient` and to the agent's
        cost callback, so a refresh reaches both without either being rebuilt.
        A merge that returned a new table would silently strand every holder of
        the old one — which is how the agent path spent its life with an empty
        price list.

        Each row carries its own `source`, so folding a hints-filled table into a
        gateway-reported one no longer relabels the gateway's rows.
        """
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
            # The model's OWN provenance. The table-level label is only a
            # fallback for a model that is not in the table, and that path
            # already returned `unknown` above.
            source=p.source,
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

    A genuinely free model — a local vLLM on owned hardware, where the marginal
    per-token charge really is nothing — is still a claim about *a deployment*,
    not about the model, so it belongs in the operator hints file
    (`input_per_token: "0"`), where it is recorded as `pricing_source="static"`.
    Asserting it here would put the claim back in the repo, which is the shape
    of table this module exists to remove.
    """
    return PricingTable()
