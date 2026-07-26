"""LiteLLM transport + ModelProfile resolver + pricing.

Every LLM and embedding call in Aleph routes through `LiteLLMClient`.
There is no other path.
"""

from aleph_models.client import (
    ChatMessage,
    ChatResponse,
    EmbedResponse,
    LiteLLMClient,
    ToolSchema,
)
from aleph_models.discovery import (
    CAPABILITY_POLICIES,
    DiscoveredModel,
    discover_models,
    probe_model,
    select_default_bindings,
    unbound_capabilities,
)
from aleph_models.pricing import CostBreakdown, PricingTable, get_default_pricing
from aleph_models.profile import (
    ResolvedBinding,
    resolve_binding,
)

__all__ = [
    "CAPABILITY_POLICIES",
    "ChatMessage",
    "ChatResponse",
    "CostBreakdown",
    "DiscoveredModel",
    "EmbedResponse",
    "LiteLLMClient",
    "PricingTable",
    "ResolvedBinding",
    "ToolSchema",
    "discover_models",
    "get_default_pricing",
    "probe_model",
    "resolve_binding",
    "select_default_bindings",
    "unbound_capabilities",
]
