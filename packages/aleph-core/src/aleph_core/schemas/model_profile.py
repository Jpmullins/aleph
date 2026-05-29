"""ModelProfile + ModelBinding schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Capability(StrEnum):
    SYNTHESIS = "synthesis"
    JUDGE = "judge"
    PAGE_SELECTION = "page_selection"
    EXTRACTION = "extraction"
    CLASSIFICATION = "classification"
    VISION = "vision"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    CODE = "code"


class ModelBindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=128)
    provider: str = Field(default="litellm", pattern=r"^litellm$")
    fallback: ModelBindingIn | None = None
    max_input_tokens: int = Field(default=200_000, ge=1, le=10_000_000)
    cost_per_input_token_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    cost_per_output_token_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    cache_discount_pct: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("100"))


class ModelBindingOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    provider: str
    fallback: ModelBindingOut | None = None
    max_input_tokens: int
    cost_per_input_token_usd: Decimal
    cost_per_output_token_usd: Decimal
    cache_discount_pct: Decimal


class ModelProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    project_id: UUID | None
    is_template: bool
    bindings: dict[str, ModelBindingOut]
    created_at: datetime
    updated_at: datetime


class ModelProfileUpdate(BaseModel):
    """PATCH body. Replaces the binding for any capability present in the payload.
    Capabilities not listed are unchanged."""

    model_config = ConfigDict(extra="forbid")

    bindings: dict[Capability, ModelBindingIn] = Field(default_factory=dict)


ModelBindingIn.model_rebuild()
ModelBindingOut.model_rebuild()
