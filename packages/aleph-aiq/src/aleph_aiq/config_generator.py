"""AIQ config generator.

Emits a per-project AIQ YAML config from the project's `ModelProfile`
and `ConnectorBinding` rows. The config is mounted into the
`aiq-server` container at `/etc/aiq/config.yml` (or overridden via the
LRU per-project cache in the AIQ wrapper).

All LLM bindings point at the Insights LiteLLM Gateway. AIQ's
`_type: openai` block treats the gateway as an OpenAI-compatible
endpoint.
"""

from __future__ import annotations

from typing import Any

import yaml

from aleph_models.profile import ResolvedBinding, resolve_binding


def _llm_block(model_name: str, base_url: str, api_key_env: str, **extras: Any) -> dict:
    return {
        "_type": "openai",
        "model_name": model_name,
        "base_url": base_url,
        "api_key": f"${{{api_key_env}}}",
        "max_retries": extras.pop("max_retries", 5),
        **extras,
    }


def emit_config(
    *,
    profile_bindings: dict[str, Any],
    enabled_connector_kinds: list[str],
    litellm_base_url: str,
    api_key_env: str = "INSIGHTS_LITELLM_API_KEY",
    otel_endpoint_env: str = "OTEL_EXPORTER_OTLP_ENDPOINT",
) -> str:
    """Return YAML text suitable for AIQ's config loader."""

    def _model_for(capability: str) -> str:
        b: ResolvedBinding = resolve_binding(profile_bindings, capability)
        return b.model

    synthesis_model = _model_for("synthesis")
    classification_model = _model_for("classification")

    llms = {
        "intent_classifier": _llm_block(
            classification_model, litellm_base_url, api_key_env, temperature=0.0
        ),
        "shallow_researcher_llm": _llm_block(
            synthesis_model, litellm_base_url, api_key_env, temperature=0.2
        ),
        "deep_orchestrator_llm": _llm_block(
            synthesis_model, litellm_base_url, api_key_env, max_retries=10
        ),
        "clarifier_llm": _llm_block(
            classification_model, litellm_base_url, api_key_env, temperature=0.0
        ),
    }

    tool_blocks: dict[str, dict] = {}
    web_search_tools: list[str] = []
    paper_search_tools: list[str] = []
    feed_tools: list[str] = []
    model_repo_tools: list[str] = []
    for kind in enabled_connector_kinds:
        slot = f"{kind}_search"
        tool_blocks[slot] = {"_type": f"{kind}_search"}
        if kind in ("tavily", "exa"):
            web_search_tools.append(slot)
        elif kind in ("serper", "arxiv", "semantic_scholar", "openalex", "lens"):
            paper_search_tools.append(slot)
        elif kind == "rss":
            feed_tools.append(slot)
        elif kind == "huggingface_hub":
            model_repo_tools.append(slot)

    functions = {
        "data_sources": {
            "_type": "data_source_registry",
            "sources": [],
        },
        **tool_blocks,
        "intent_classifier": {"_type": "intent_classifier", "llm": "intent_classifier"},
        "shallow_researcher": {
            "_type": "shallow_researcher",
            "llm": "shallow_researcher_llm",
        },
        "deep_researcher": {"_type": "deep_researcher", "llm": "deep_orchestrator_llm"},
        "clarifier": {"_type": "clarifier", "llm": "clarifier_llm"},
    }
    sources = functions["data_sources"]["sources"]
    if web_search_tools:
        sources.append({"id": "web_search", "name": "Web Search", "tools": web_search_tools})
    if paper_search_tools:
        sources.append(
            {
                "id": "paper_search",
                "name": "Academic Papers",
                "tools": paper_search_tools,
            }
        )
    if feed_tools:
        sources.append({"id": "feed", "name": "RSS Feeds", "tools": feed_tools})
    if model_repo_tools:
        sources.append({"id": "model_repo", "name": "Model Repos", "tools": model_repo_tools})

    config = {
        "general": {
            "telemetry": {
                "tracing": {
                    "otel": {
                        "_type": "otlp",
                        "endpoint": f"${{{otel_endpoint_env}}}",
                    }
                }
            }
        },
        "llms": llms,
        "functions": functions,
    }
    return yaml.safe_dump(config, sort_keys=False)
