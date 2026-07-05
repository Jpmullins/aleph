# LiteLLM transport

The Insights LiteLLM Gateway is the **only** path for LLM and embedding
calls. Every package that needs to call a model imports `LiteLLMClient`
from `aleph-models`. No provider SDK is imported anywhere else in Aleph
or in AIQ.

## Configuration

Two env vars:

```
LITELLM_BASE_URL=https://gateway.insights.arlis.umd.edu
INSIGHTS_LITELLM_API_KEY=<bearer>
```

Both are required at boot. The API's `/readyz` calls
`LiteLLMClient.health()` (which hits `/v1/models`); a 503 surfaces if
the gateway is unreachable. Project creation does **not** require the
gateway — only operations that hit a model do.

## Calling chat

```python
from aleph_models.client import ChatMessage
from aleph_core.schemas.model_profile import Capability

resp = await litellm.chat(
    principal=principal,
    project_id=project_id,
    agent_run_id=agent_run_id_or_None,
    capability=Capability.SYNTHESIS,
    profile_bindings=profile.bindings_jsonb,
    messages=[ChatMessage(role="user", content="...")],
    purpose="wiki.page.compose",
    max_tokens=2048,
)
```

What the client does, in order:

1. Resolves `capability → ModelBinding` from the project's `ModelProfile`.
2. Starts an OTEL span (`litellm.chat`) tagged with `aleph.project_id`,
   `aleph.capability`, `aleph.model`, `aleph.purpose`, plus the
   `gen_ai.*` semantic conventions Langfuse 4 understands.
3. POSTs `/v1/chat/completions` with tenacity retry (3 attempts,
   exponential backoff, retry on 5xx + 429 + connection errors).
4. Computes `cost_usd` from `pricing.py` (cache-discount-aware).
5. Inserts a `ModelCall` row and a `CostLedgerEvent` row in one
   transaction. The budget rollup trigger updates `budgets.spent_usd`.
6. Returns a `ChatResponse` carrying `cost_usd`, `model_call_id`,
   `trace_id`, `latency_ms`, plus the OpenAI-shaped choices/usage.

## Idempotency

Pass `idempotency_key=...` for state-changing calls (e.g. wiki proposals).
The key is stored in Redis with a 24h TTL bound to the resulting
`model_call_id`. A duplicate call replays without re-charging the gateway.

## Pricing

`aleph_models.pricing.PricingTable` ships per-model rates verified
against the gateway model list 2026-05-27. Updates are PRs — never
runtime. Unknown models cost zero and emit a Langfuse alert via the span.

## What `aleph-api` and `aleph-workers` share

Both processes construct a `LiteLLMClient` in their startup hook. The
client is stateless modulo:

- `httpx.AsyncClient` — per-process pool
- `redis.asyncio.Redis` — per-process pool, idempotency cache
- `session_maker` — per-process async sessionmaker

The same client is reused across many calls and across many agent runs.

## Gateway-side gotcha: `parallel_tool_calls` + Bedrock

The Insights gateway routes every model to `bedrock/global.anthropic.*`.
LangChain's `ChatOpenAI` (used by the agent path and by NVIDIA AIQ) sends
`parallel_tool_calls` by default. LiteLLM treats it as "supported" for Bedrock
and mis-folds it into a malformed `tool_choice`, so Bedrock 400s with
`"tool_choice.type: Field required"` — breaking every tool-calling turn.

Fix lives in the gateway repo (`~/code/ARLIS/insights-k8s-manifests`,
`litellm/proxy-config.yaml`): each chat model's `litellm_params` carries
`additional_drop_params: ["parallel_tool_calls"]`. Note: a global
`litellm_settings.additional_drop_params` and bare `drop_params: true` do NOT
drop it — it must be **per-model**. If you add a new model to the gateway,
add that line too. (Aleph's own `LiteLLMClient` path doesn't hit this — it's
specific to the LangChain/AIQ clients that emit `parallel_tool_calls`.)
