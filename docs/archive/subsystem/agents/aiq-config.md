# AIQ config generator

`aleph_aiq.config_generator.emit_config` produces a YAML config for
the AIQ server, per project. The config:

- Wires every `llms.*` block to `_type: openai` with
  `base_url=${LITELLM_BASE_URL}` and `api_key=${INSIGHTS_LITELLM_API_KEY}`.
- Resolves model names from the project's `ModelProfile`. Synthesis
  capability maps to `claude-opus-4-7` under `aleph-production`,
  `claude-sonnet-4-6` under `aleph-dev`.
- Builds `functions.data_sources` from the project's enabled
  `ConnectorBinding`s. Disabled connectors are not present in the
  config — AIQ literally cannot call them.
- Points OTEL at the same collector Aleph uses, so AIQ traces flow
  into Langfuse alongside Aleph's.

The config is regenerated when the project's `ModelProfile` or
`ConnectorBinding`s change; the AIQ wrapper maintains an LRU cache
keyed by `project_id`.

## Sample (truncated)

```yaml
general:
  use_uvloop: true
  # Telemetry is intentionally omitted — the 2.1.0 image rejects unknown
  # discriminator `_type`s (the old `otlp` value is invalid; the valid tag is
  # `nat.plugins.opentelemetry/otelcollector`). Aleph already receives OTEL
  # spans from aleph-api/aleph-workers, so AIQ tracing is left off for now.

llms:
  intent_classifier:
    _type: openai
    model_name: claude-haiku-4-5
    base_url: https://gateway.insights.arlis.umd.edu
    api_key: ${INSIGHTS_LITELLM_API_KEY}
  shallow_researcher_llm:
    _type: openai
    model_name: claude-opus-4-7
    base_url: https://gateway.insights.arlis.umd.edu
    api_key: ${INSIGHTS_LITELLM_API_KEY}
  # ... deep_orchestrator_llm, clarifier_llm

functions:
  data_sources:
    _type: data_source_registry
    sources:
      - id: web_search
        name: Web Search
        tools: [tavily_search, exa_search]
      - id: paper_search
        name: Academic Papers
        tools: [arxiv_search, openalex_search, ...]
  tavily_search: {_type: tavily_search}
  arxiv_search: {_type: arxiv_search}
  # ...
```
