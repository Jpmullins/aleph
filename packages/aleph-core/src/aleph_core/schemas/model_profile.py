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


class GatewayModelOut(BaseModel):
    """A model the gateway advertises, as offered to the Settings picker.

    Aleph ships no model list; this is read from the gateway's own
    `/model/info` at runtime. `capabilities` is computed server-side from the
    same policy that generates defaults, so the UI never has to reimplement
    (and drift from) the rules about which models can do which job.
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str
    mode: str | None = None
    max_input_tokens: int | None = None
    input_per_token: Decimal | None = None
    output_per_token: Decimal | None = None
    supports_vision: bool = False
    supports_function_calling: bool = False
    supports_reasoning: bool = False
    supports_prompt_caching: bool = False
    #: False when the gateway advertises no price. Such a model is shown but
    #: never auto-selected — binding it would record calls at a silent $0.
    is_priced: bool = True
    capabilities: list[str] = Field(default_factory=list)


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
