"""ModelProfile resolution.

Capability → ModelBinding lookup. Reads from a ModelProfile row's
`bindings_jsonb` (validated through Pydantic at write time, so we can
trust the shape on read).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aleph_core.errors import ValidationFailed
from aleph_core.schemas.model_profile import Capability


@dataclass(frozen=True)
class ResolvedBinding:
    model: str
    provider: str
    max_input_tokens: int
    cost_per_input_token_usd: Decimal
    cost_per_output_token_usd: Decimal
    cache_discount_pct: Decimal
    fallback: "ResolvedBinding | None" = None


def _decimal(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if v is None:
        return Decimal("0")
    return Decimal(str(v))


def _build(b: dict[str, Any]) -> ResolvedBinding:
    fb = b.get("fallback")
    return ResolvedBinding(
        model=b["model"],
        provider=b.get("provider", "litellm"),
        max_input_tokens=int(b.get("max_input_tokens", 200_000)),
        cost_per_input_token_usd=_decimal(b.get("cost_per_input_token_usd")),
        cost_per_output_token_usd=_decimal(b.get("cost_per_output_token_usd")),
        cache_discount_pct=_decimal(b.get("cache_discount_pct")),
        fallback=_build(fb) if fb else None,
    )


def resolve_binding(
    bindings: dict[str, Any], capability: Capability | str
) -> ResolvedBinding:
    key = capability.value if isinstance(capability, Capability) else capability
    raw = bindings.get(key)
    if raw is None:
        msg = f"model profile has no binding for capability '{key}'"
        raise ValidationFailed(msg)
    return _build(raw)
