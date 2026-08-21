# Connecting to OpenAI-compatible model endpoints, properly, in 2026

Research file for the Aleph redesign. Written 19 August 2026.
All version numbers below were verified live against PyPI, npm, GitHub and vendor docs on
2026-08-19 unless explicitly marked *unverified*.

---

## In one paragraph

"OpenAI-compatible" is a marketing claim, not a contract. Every server Aleph might be pointed at —
LiteLLM proxy, Ollama, vLLM, llama.cpp, TGI, OpenRouter, Bedrock shims, hosted APIs — implements a
different *subset* of the OpenAI HTTP API, and the dangerous cases are not the ones that return an
error. They are the ones that accept your request, return HTTP 200, and quietly drop the thing you
asked for: vLLM accepts `strict: true` on a tool definition and ignores it; Ollama accepts a request
with `tool_choice` and ignores it; a streaming call without `stream_options.include_usage` returns a
perfectly good answer with no token counts, so the call costs money and is recorded as $0. The
correct architecture is therefore *not* "write to the OpenAI spec and hope" and *not* "hardcode a
provider list". It is: keep one thin HTTP transport of your own; attach to every endpoint a **capability
record** that says what that endpoint actually does, built from three stacked sources (what the
gateway reports about itself, a fetched public catalog, operator overrides) and **confirmed by a live
probe** before anything is bound; degrade explicitly and loudly when a capability is missing; and
never let a call complete without a usage row, even if you had to estimate it and label the estimate.
Aleph already has most of the right instincts here — `aleph_models.discovery`, `hints.py`,
`pricing_source` — and the work left is mostly to (a) stop hardcoding embedding dimensions,
(b) move the endpoint's capability facts into an explicit, probed record instead of implicit
assumptions scattered through call sites, and (c) close the streaming/usage hole.

---

## 0. Verified version snapshot (2026-08-19)

| Thing | Version | Date | How verified |
|---|---|---|---|
| `openai` (Python SDK) | **3.3.0** | 2026-08-18 | PyPI JSON API |
| `openai` (Node SDK) | **7.5.0** | 2026-08-17 | npm registry |
| `litellm` (SDK + proxy) | **1.97.0** | 2026-08-16 | PyPI JSON API |
| `vllm` | **0.27.1** | 2026-08-11 | PyPI JSON API |
| Ollama | **v0.32.14** | 2026-08-15 | GitHub releases API |
| `ai` (Vercel AI SDK) | **7.0.68** | 2026-08-18 | npm registry |
| `@ai-sdk/openai-compatible` | **3.0.31** | 2026-08-17 | npm registry |
| `@ai-sdk/gateway` | **4.0.54** | 2026-08-18 | npm registry |
| models.dev catalog | live | fetched 2026-08-19 | `https://models.dev/api.json` — 192 providers, 6,838 models, 4.0 MB |
| LiteLLM price catalog | live | fetched 2026-08-19 | `model_prices_and_context_window.json` — 3,055 entries |
| Aleph today | `aleph-models` on raw `httpx==0.28.1` + `tenacity==9.1.4` | — | repo read |

Two facts about the cadence matter more than any individual number:

- **The OpenAI Python SDK went 2.54.0 → 3.0.0 → 3.3.0 in six days** (12–18 Aug 2026). v3.0.0's
  breaking change is that **`httpx2` replaced `httpx` as the default HTTP client and `httpx` is no
  longer installed**. Anything that shares an `httpx` client, transport, or `Timeout` object with the
  SDK breaks on that upgrade. Aleph pins `httpx==0.28.1` today, so adopting the OpenAI SDK now means
  taking on `httpx2` across the Python workspace or running two HTTP stacks.
- **vLLM ships roughly every two weeks** and its OpenAI frontend changes in most releases. Any
  capability table you write down about vLLM is stale within a month. This is the strongest possible
  argument for probing over asserting.

---

## 1. The compatibility reality

### 1.1 The failure mode that matters is silent, not loud

Three categories, in increasing order of danger:

1. **Loud incompatibility** — the server 400s on an unknown field. Annoying, easy to handle, cheap.
2. **Shape divergence** — the server returns a slightly different response body (reasoning in
   `reasoning_content` instead of `reasoning`, tool arguments as a dict instead of a JSON string,
   `content: null` where you expected `""`). Caught by a strict parser; Aleph already handles the
   `content: null` case explicitly in `LiteLLMClient`.
3. **Accepted-and-ignored** — the server takes your parameter, returns 200, and does not honour it.
   **This is the class that produces shipped-but-inert features**, which is exactly the defect
   class `CLAUDE.md` already names as Aleph's dominant one.

Documented instances of category 3, verified:

- **vLLM does not implement `strict`.** From vLLM's own tool-calling docs: the `strict` field "is
  accepted in requests to maintain client compatibility, [but] has no effect on vLLM's decoding
  behavior." In `tool_choice: "auto"` mode, argument validity depends entirely on the model and the
  text parser. If you rely on `strict: true` to guarantee schema-valid tool arguments, on vLLM you
  have no guarantee at all and no error telling you so.
- **Ollama does not implement `tool_choice`.** Ollama's compatibility page lists tools as supported
  and `tool_choice` as not implemented. Forced tool calling — "you must call `search` now" — silently
  becomes optional tool calling. An agent loop built on forced calls degrades into one that sometimes
  just talks.
- **Ollama does not implement `logprobs`** on either chat or completions.
- **LiteLLM proxy currently fails to write a spend-log row for streaming `/v1/responses` calls**
  (BerriAI/litellm issue #32487: the streaming success logger crashes on a dict without `.usage`, so
  no `LiteLLM_SpendLogs` row is written and the request goes uncharged). A gateway that is supposed
  to be your accounting authority can silently stop accounting.
- **`x-litellm-response-cost` is unreliable while streaming.** Headers must flush before the body, so
  the cost header on a streamed response is a pre-stream estimate, not the settled cost
  (BerriAI/litellm issue #12689).

### 1.2 Chat Completions vs the Responses API

**What they are.** `/v1/chat/completions` is the older, stateless "here is a list of messages, give me
the next one" endpoint. `/v1/responses` is OpenAI's newer endpoint: it models a *response* as an
object with typed output items (text, reasoning, tool calls, built-in tool results), it can be
stateful server-side (`store: true`, `previous_response_id`), and it carries encrypted reasoning state
so a reasoning model can continue a chain across turns without the client seeing the raw reasoning.

**Where the ecosystem actually is, August 2026:**

- OpenAI has **not** deprecated Chat Completions and says it has no plan to. It does say new
  capabilities land in Responses first. The Assistants API — a different thing — is being retired
  26 August 2026, i.e. *next week*.
- OpenAI's own Codex CLI **removed** Chat Completions support in early February 2026. That is the
  clearest signal of direction inside OpenAI: their flagship agent is Responses-only.
- **vLLM 0.27.x implements `/v1/responses`** (`/v1/responses`, `/v1/responses/{id}`,
  `/v1/responses/{id}/cancel`) and it works with the official OpenAI client.
- **Ollama implements `/v1/responses` but non-stateful only** — streaming, tools and reasoning
  summaries work; "stateful requests" are explicitly unsupported. So `previous_response_id` and
  `store: true` are out.
- **LiteLLM proxy exposes `/responses`** alongside `/chat/completions`, `/embeddings`, `/rerank`,
  `/messages` (Anthropic shape), `/images`, `/audio`, `/batches`.
- llama.cpp's server and TGI remain Chat-Completions-shaped. TGI is widely described as being in
  maintenance mode (*unverified* — I could not confirm this from HuggingFace directly, treat as
  ecosystem opinion, but it matches the absence of TGI from every 2026 comparison's "recommended"
  column).

Two real systems split the difference by treating them as **two different wire protocols, not two
options on one client**:

- **prime-agent's `@earendil-works/pi-ai` (v0.7.3)** has separate provider modules
  `openai-completions.ts` (1,167 lines) and `openai-responses.ts` (295 lines), plus
  `openai-codex-responses.ts` and `azure-openai-responses.ts`. Each model record carries an `api`
  field naming which protocol it speaks, and a registry dispatches on it.
- **hermes-agent (Python, `openai==2.24.0`)** has `chat_completion_helpers.py` for the completions
  path and a separate `codex_responses_adapter.py` / `codex_runtime.py` for the Responses path.

**Practical reading for Aleph:** Chat Completions is the only protocol you can assume across every
endpoint an operator might point you at. Responses is where reasoning-model features (encrypted
reasoning replay, server-side conversation state, built-in tools) live and where OpenAI's own agent
tooling has moved. Design the transport so the protocol is a *property of the resolved model binding*,
not a global setting — because the same deployment will have some models reachable only over one and
some only over the other.

### 1.3 Streaming formats

The wire format is server-sent events (SSE): lines of `data: {json}` terminated by `data: [DONE]`.
That much is genuinely universal. What differs:

- **Whether `[DONE]` is sent at all.** Some proxies close the stream without it.
- **Whether errors arrive as HTTP status codes or as SSE frames.** hermes-agent has a dedicated
  exception class, `ProviderStreamError`, whose docstring reads: *"Provider encoded an API error as
  streaming content instead of an SDK error."* It also has a synthetic error code
  `provider_stream_non_json_data` for when the SDK rejects a provider's `data:` field before any
  completion chunk arrives. If you only catch HTTP errors, these look like a successful empty response.
- **Whether a `finish_reason` you have never seen appears.** prime-agent maps `content_filter` and
  `network_error` as finish reasons and has a default arm that surfaces `Provider finish_reason: <x>`
  as an error rather than treating it as a normal stop.
- **Whether the server accepts non-streaming requests at all.** hermes-agent carries a
  `_provider_requires_stream()` helper because Tencent Copilot returns
  `{"code": 11101, "msg": "Non-stream chat request is currently not supported"}` — and exposes a
  config key `auxiliary.stream_only_base_urls` so an operator can mark any custom endpoint stream-only.
- **Whether `stream_options` is accepted.** hermes has `_is_streaming_rejected_error()` which
  string-matches `"stream_options"` in the error text, then retries the identical request without it.
  Google's native Gemini REST endpoint rejects `stream_options` outright; hermes has a test named
  `test_native_gemini_endpoint_omits_stream_options` pinning that.
- **Long silent gaps.** A reasoning model can think for two minutes before emitting a token, and
  load balancers close connections that look idle. Servers mitigate with SSE comment frames
  (`: ping`). Clients must mitigate with an **idle timeout** rather than a total timeout — see §3.

**opencode's implementation is the pattern worth copying.** In
`packages/opencode/src/provider/provider.ts` it wraps the SSE `Response` body in a `ReadableStream`
whose `pull` races each `reader.read()` against a timer; on expiry it raises
`ProviderError.ResponseStreamError("SSE read timed out")`, aborts the `AbortController`, and cancels
the reader. The default is `OPENAI_HEADER_TIMEOUT_DEFAULT = 300_000` (5 minutes). That is a
**per-chunk stall timeout**, not a request deadline — the only timeout shape that is correct for
streaming generation.

### 1.4 Tool / function calling — the biggest divergence

This is where "compatible" breaks down hardest. Dimensions of divergence:

| Dimension | What differs |
|---|---|
| `tool_choice: "auto"` | Universal. |
| `tool_choice: "required"` | vLLM: constrained decoding, valid JSON guaranteed. Ollama: **not implemented**. Many aggregators: silently ignored. |
| `tool_choice: {type:"function", name:...}` (named/forced) | vLLM: constrained decoding. Ollama: **not implemented**. |
| `parallel_tool_calls: false` | vLLM implements it by **post-filtering to the first call** (`maybe_filter_parallel_tool_calls`), not by constraining generation. Cost is still paid for the discarded calls. |
| `strict: true` on the function schema | OpenAI: enforced. **vLLM: accepted and ignored.** Moonshot/Kimi, Cloudflare AI Gateway, Prime Inference: prime-agent sets `supportsStrictMode: false`. |
| Tool result message shape | Some providers require a `name` field on the tool result message (`requiresToolResultName`). |
| Message ordering | Some providers reject a `user` message directly after a `tool` result and need an assistant turn in between (`requiresAssistantAfterToolResult`). |
| `content: null` on assistant turns | Some providers reject null and need `""`. Some require "either content or tool_calls, but not none". |
| Tool arguments type | JSON string per spec; a few return an object. |
| Streaming tool deltas | z.ai needs a top-level `tool_stream: true` to stream tool-call deltas at all (`zaiToolStream`). |
| JSON Schema dialect | **llama.cpp's json-schema-to-grammar rejects regex escapes in `pattern` / `format`** — hermes-agent has a dedicated failover reason `llama_cpp_grammar_pattern` whose recovery is "strip those keywords from the tools and retry." |

**How vLLM actually behaves** (from its serving code, verified via docs): tool handling branches on
whether the loaded tool-call parser class declares `supports_required_and_named`. If it does,
`required` and named choices use JSON-schema-constrained decoding — genuinely reliable. If it does
not, those modes fall back to the *text parser*, which extracts tool calls out of free-form model
output with no guarantee. Whether you get the strong or weak behaviour depends on which
`--tool-call-parser` the operator passed at server start, which you cannot see from the API.
Also, `--enable-auto-tool-choice` must be set at server start or `tool_choice: "auto"` produces no
tool calls at all.

**The takeaway.** Tool calling reliability is a property of *(server, model, server launch flags)* and
none of those three are fully discoverable over HTTP. Aleph's agent loop must be written to survive
the weak case: validate every tool call's arguments against the schema client-side, treat a
malformed call as a recoverable turn (feed the validation error back as a tool result) rather than a
crash, and never assume a forced call was actually forced.

### 1.5 Structured output / JSON schema mode

Three tiers, and they are not interchangeable:

1. **`response_format: {"type": "json_object"}`** ("JSON mode") — output is *some* valid JSON, schema
   not enforced. Supported essentially everywhere, including Ollama and llama.cpp.
2. **`response_format: {"type": "json_schema", "json_schema": {...}}`** — output conforms to your
   schema. vLLM supports this via constrained decoding. Backend is selectable
   (`xgrammar` default via `auto`, plus `guidance`, `outlines`, `lm-format-enforcer`) and **the
   backends do not accept the same schemas**: vLLM has per-backend validators
   (`validate_xgrammar_grammar`, `validate_guidance_grammar`, …) and raises on unsupported features —
   e.g. the `guidance` backend refuses non-tekken Mistral tokenizers outright.
3. **`strict: true`** — OpenAI's stricter subset (no `additionalProperties: true`, all properties
   required, limited keyword set) with a hard guarantee. **Rarely honoured off OpenAI.**

Practical rule: request tier 2 when the endpoint advertises it, always validate the returned JSON
client-side anyway, and have a repair path (re-prompt with the validation error) rather than
treating a schema violation as an outage. LiteLLM's own catalog carries a `supports_response_schema`
boolean per model, which is a usable discovery signal.

### 1.6 Embeddings

Covered in full in §6 — it deserves its own section because it is where Aleph has a live hazard.
The short version: `/v1/embeddings` is widely implemented, but `dimensions`, `encoding_format`,
token-array input, batch limits and normalization all differ, and **nothing in the API tells you the
output dimension until you make a billed call.**

### 1.7 Reranking

There is **no OpenAI reranking endpoint**, so there is no "OpenAI-compatible" rerank. The de facto
standard is **Cohere's `/v1/rerank`**, and Jina's is near-identical with extra response fields.

- vLLM serves `/rerank`, `/v1/rerank` and `/v2/rerank`, explicitly compatible with **both** Jina's
  and Cohere's shapes, plus a lower-level `/score` endpoint that returns raw pair scores
  (cross-encoder score, or cosine similarity if the model is an embedder).
- LiteLLM proxy exposes `/rerank` and can be configured as a pass-through to Cohere.
- Ollama, llama.cpp and TGI: no rerank endpoint in the OpenAI namespace.
- Dedicated servers — HuggingFace text-embeddings-inference, Infinity — serve rerank on their own
  routes.

So reranking must be modelled as a **separate, optional capability with its own base URL**, not as
something you can assume lives on the chat gateway.

### 1.8 Vision / multimodal inputs

`messages[].content` as an array of `{type: "text"}` / `{type: "image_url"}` parts is the common
shape. Divergences:

- **Ollama accepts base64 data URIs but not remote image URLs.** If you pass an `https://` image
  URL, it will not fetch it.
- PDF input (`type: "file"` / `input_file`) is an OpenAI/Anthropic-era addition supported by very
  few OpenAI-compatible servers. LiteLLM's catalog tracks it as `supports_pdf_input`.
- Per-image size limits differ, and exceeding them is a distinct error worth its own recovery path —
  hermes-agent has `image_too_large` as a first-class failover reason whose recovery is "shrink and
  retry", separate from generic `payload_too_large` (HTTP 413).
- Whether images survive a *replay* of the conversation differs; some providers reject list-type
  content in tool-result messages entirely (hermes: `multimodal_tool_content_unsupported`, observed
  on Xiaomi MiMo, recovery is "downgrade to text and retry").

### 1.9 Prompt caching

Prompt caching means the server keeps the KV-cache for a repeated prompt prefix and charges less for
it. There is **no single API**:

- **OpenAI-style**: implicit (automatic on long prefixes), reported back as
  `usage.prompt_tokens_details.cached_tokens`. Newer OpenAI models add explicit control:
  `prompt_cache_key`, `prompt_cache_retention: "24h"`, and explicit cache *breakpoints* on content
  blocks (the AI SDK exposes these as `promptCacheKey`, `promptCacheRetention`,
  `promptCacheOptions: {mode: 'implicit'|'explicit', ttl}` and `promptCacheBreakpoint`).
- **Anthropic-style**: explicit `cache_control` markers on the system prompt, last tool definition,
  and last content block, with a `ttl`. Aggregators that front Anthropic models over an
  OpenAI-shaped API (OpenRouter, Prime Inference) require the Anthropic markers *inside* an
  OpenAI-shaped body — prime-agent models this as `cacheControlFormat: "anthropic"`.
- **Session affinity**: some providers only hit cache if you send a stable session header
  (`session_id`, `x-client-request-id`, `x-session-affinity`) — prime-agent's
  `sendSessionAffinityHeaders`.
- **Self-hosted (vLLM prefix caching)**: caching happens, but **the API does not report cached
  tokens** in OpenAI's `cached_tokens` field in general. You save latency and GPU, and your accounting
  cannot see it. (vLLM 0.27.0 added `num_cache_creation_tokens` on its *Anthropic Messages* surface,
  not the OpenAI one.)
- **Misreporting**: prime-agent's code carries a note that some providers via OpenRouter report
  `cached_tokens` in a way that double-counts against `prompt_tokens` — so the arithmetic
  `billable_input = prompt_tokens - cached_tokens` is not universally safe.

### 1.10 Usage and token accounting

**Non-streaming**: `usage` is present essentially everywhere. `prompt_tokens_details.cached_tokens`
and `completion_tokens_details.reasoning_tokens` are much less consistent.

**Streaming**: usage is **withheld by default**. You must send
`stream_options: {"include_usage": true}` and then read the *final* chunk, which has an empty
`choices` array and a populated `usage`. Notes:

- Ollama supports `stream_options.include_usage`.
- Google's native Gemini REST endpoint **rejects** `stream_options` (400).
- Some proxies accept it and never emit the usage chunk.
- If the stream is cut short — client abort, network drop, idle timeout — **the usage chunk never
  arrives**, so tokens were generated and billed upstream with no local record.

This is the direct cause of Aleph's known cost-attribution hole (`AgentCostCallbackHandler` writes a
`ModelCall` only when usage is present), and it is not fixable by "remember to set the flag" — the
abort case defeats that. See §4.

### 1.11 Compatibility summary table

Best-effort as of 2026-08-19. **Y** = works; **P** = partial / model-dependent / launch-flag
dependent; **N** = not implemented; **ign** = accepted but ignored (the dangerous cell).

| Feature | OpenAI | LiteLLM proxy | vLLM 0.27 | Ollama 0.32 | llama.cpp | TGI | OpenRouter | Bedrock shim |
|---|---|---|---|---|---|---|---|---|
| `/v1/chat/completions` | Y | Y | Y | Y | Y | Y | Y | Y |
| `/v1/responses` | Y | Y | Y | P (stateless only) | N | N | P* | P* |
| SSE streaming | Y | Y | Y | Y | Y | Y | Y | P |
| `stream_options.include_usage` | Y | Y | Y | Y | P | P | Y | P |
| tools (`auto`) | Y | Y | P (needs `--enable-auto-tool-choice` + parser) | P | P | P | P | P |
| `tool_choice: required` / named | Y | P | P (parser-dependent) | **N** | P | P | P | P |
| `parallel_tool_calls: false` | Y | P | P (post-filter) | P | P | P | P | P |
| `strict: true` | Y | P | **ign** | P | P | P | P | P |
| `response_format: json_object` | Y | Y | Y | Y | Y | Y | Y | P |
| `response_format: json_schema` | Y | P | Y (backend-dependent) | P | Y (GBNF) | P | P | P |
| `/v1/embeddings` | Y | Y | Y (embedding model only) | Y (embedding model only) | Y | P | P | P |
| `dimensions` param | Y | P | P | Y | N | N | P | P |
| rerank | N | Y (`/rerank`) | Y (`/rerank`, Cohere+Jina) | N | N | N | P | P |
| vision (image_url) | Y | Y | P | P (base64 only) | P | P | P | P |
| prompt caching reported | Y | P | N (OpenAI surface) | N | N | N | P | P |
| `logprobs` | Y | P | Y | **N** | Y | P | P | N |
| `n > 1` | Y | P | Y | **N** | P | P | P | N |

\* Aggregator/shim support depends entirely on the specific configuration; treat as unknown until probed.

**The row that should decide your architecture is the `strict` row.** One "ign" cell is enough to
justify never trusting a documented capability without a probe.

---

## 2. Which client library

### 2.1 What production systems actually use (direct evidence from the local codebases)

| System | Language | LLM transport | Notes |
|---|---|---|---|
| **opencode** | TypeScript / Bun | **Vercel AI SDK** (`ai` 6.0.168 in-repo) with ~20 `@ai-sdk/*` provider packages incl. `@ai-sdk/openai-compatible` 2.0.41 and `@ai-sdk/gateway` | Providers are **lazily dynamic-imported** by npm package name, and unknown providers can be **installed from npm at runtime** (`Npm` service). Model metadata comes from **models.dev**, cached on disk with a flock. |
| **prime-agent** | TypeScript / Node | **Own library** `@earendil-works/pi-ai` 0.7.3 wrapping `openai` **6.47.0** + `@anthropic-ai/sdk` + `@google/genai` + `@mistralai/mistralai` + `@aws-sdk/client-bedrock-runtime` | Explicit per-protocol provider modules; an `api-registry` keyed by protocol name with `registerApiProvider` / `unregisterApiProviders(sourceId)` — i.e. **runtime-pluggable providers**. Model list is code-generated (`models.generated.ts`). |
| **hermes-agent** | Python | **Official `openai==2.24.0` SDK**, exact-pinned, + `httpx[socks]==0.28.1`, `tenacity==9.1.4` | Provider-specific adapters (`anthropic_adapter.py`, `bedrock_adapter.py`, `gemini_native_adapter.py`, `codex_responses_adapter.py`) sit beside the OpenAI path. Enormous investment in error classification and failover. |
| **Aleph today** | Python | **Raw `httpx==0.28.1`** + `tenacity`, no SDK | ~540-line `LiteLLMClient`, hand-rolled Pydantic wire types. |

Two things jump out. First, **nobody uses the LiteLLM Python SDK in-process.** Not one of the three.
LiteLLM appears as a *proxy* (a separate container), never as a client library. Second, **everyone
either uses the official SDK or writes their own transport — and everyone writes their own
compatibility layer on top either way.** The SDK gets you HTTP correctness; it does not get you
compatibility.

Also note hermes-agent's dependency comment, which is directly relevant to Aleph's supply-chain
posture: they exact-pin every direct dependency (`==X.Y.Z`, never ranges) explicitly because the
"Mini Shai-Hulud" worm hit `mistralai` 2.4.6 on PyPI on 2026-05-12 and a range would have pulled it.

### 2.2 The four options, honestly

**(a) Official `openai` Python SDK pointed at `base_url`.**
- **For:** correct HTTP semantics for free — `max_retries` (default 2) with jittered exponential
  backoff, `Retry-After` honoured up to two minutes (added in 2.52.0), granular `httpx2.Timeout`
  (connect/read/write), per-request `.with_options(...)` overrides, streaming helpers, typed models.
  Tracks OpenAI's own API surface (Responses, encrypted reasoning, cache breakpoints) the day it ships.
- **Against:** **v3.0.0 (2026-08-12) made `httpx2` the default and stopped installing `httpx`.**
  Aleph pins `httpx==0.28.1` across `aleph-models`, `aleph-scholar` and `aleph-connectors`; adopting
  the SDK means either migrating all of them or carrying two HTTP stacks in one process. The release
  cadence (three majors' worth of change in a week) is a maintenance tax. It also validates responses
  against OpenAI's schema, which is *stricter than reality* on non-OpenAI servers — a mixed blessing.
- **Verdict:** the default choice for a greenfield Python agent. For Aleph it is a real migration,
  not a drop-in.

**(b) LiteLLM Python SDK in-process.**
- **For:** one function call reaches 100+ providers; ships `model_prices_and_context_window.json`
  (3,055 entries) for costing; has retry/fallback built in.
- **Against:** it is a very large dependency that pulls a large transitive tree into a process that
  is already the composition root for a kernel. It duplicates the job of the proxy Aleph already runs.
  Its abstractions leak (provider prefixes in model names). **Zero of the three reference systems use
  it in-process.** And its "handles everything" surface is precisely the kind of thing that hides a
  category-3 silent divergence from you.
- **Verdict:** no. Keep LiteLLM where it belongs — as an optional external proxy the operator may point
  Aleph at.

**(c) LiteLLM *proxy* as the only endpoint Aleph knows.**
- This is Aleph's current architecture, and it is a genuinely good default: it centralises keys,
  budgets, fallbacks, cooldowns, spend logs, and it makes the client trivially simple.
- **But it must not be a requirement.** The constraint says Aleph connects to "LiteLLM, Ollama, vLLM,
  Bedrock, OpenRouter, etc." — the moment you point it at raw Ollama, everything the proxy was doing
  for you (cost, fallback, retry policy, virtual keys) is gone. If the proxy's features are load-bearing,
  Aleph is broken on every other endpoint. **Assume the dumbest endpoint; use the smart one's features
  as an optimisation, never as a dependency.** LiteLLM's spend-log bug on streaming `/v1/responses`
  (§1.1) is a live demonstration of why you keep your own books regardless.

**(d) Your own thin HTTP client (what Aleph has).**
- **For:** total control over the compat layer, no dependency risk, no version churn, and — for
  Aleph specifically — no `httpx2` migration. `httpx` + `tenacity` is a completely respectable
  transport; hermes-agent uses the same two libraries *under* the SDK.
- **Against:** you re-implement retry policy, `Retry-After` parsing, connection pooling defaults,
  SSE parsing, and the long tail of streaming edge cases. Aleph's current `gateway_retry()` is
  33 lines and — see §3 — already has real gaps.
- **Verdict:** **keep it, but promote it.** The compat layer is the valuable part and no SDK gives it
  to you. What Aleph should steal from the SDK is the *policy*, not the code: `Retry-After`
  honouring, granular timeouts, idle-based stream timeouts, `x-request-id` capture.

### 2.3 Recommendation for Aleph

Keep the hand-rolled `httpx` transport as the single chokepoint, and add the layer that is currently
missing: an explicit **`EndpointProfile`** — a typed record of what one base URL can actually do,
resolved once per endpoint and cached, consulted by the request builder. This is exactly
prime-agent's `ResolvedOpenAICompletionsCompat` and it is the single most transferable idea in any of
the three codebases.

Its fields, from prime-agent's real list (each one exists because something broke):

```
supportsStore                                # accepts `store`
supportsDeveloperRole                        # `developer` vs `system` role
supportsReasoningEffort                      # accepts `reasoning_effort`
supportsUsageInStreaming                     # accepts stream_options.include_usage
maxTokensField                               # "max_completion_tokens" | "max_tokens"
requiresToolResultName                       # tool result msg needs `name`
requiresAssistantAfterToolResult             # no user msg directly after a tool result
requiresThinkingAsText                       # thinking blocks must become <thinking> text
requiresReasoningContentOnAssistantMessages  # replayed assistant msgs need reasoning_content
thinkingFormat                               # openai | openrouter | deepseek | zai | qwen | qwen-chat-template
supportsStrictMode                           # `strict` on tool schemas is honoured
cacheControlFormat                           # undefined | "anthropic"
sendSessionAffinityHeaders                   # session_id / x-session-affinity for cache hits
supportsLongCacheRetention                   # prompt_cache_retention "24h" / cache_control ttl "1h"
zaiToolStream                                # top-level tool_stream:true needed for tool deltas
```

Crucially, prime-agent resolves this in two steps: `detectCompat(model)` derives defaults from the
provider name and base URL, then `getCompat(model)` lets an explicit per-model `compat` object
override any field. Aleph should add a third step — **probe** — because Aleph's operators point at
arbitrary endpoints that no heuristic will recognise, and Aleph already has `probe_model` to build on.

---

## 3. Reliability

### 3.1 Timeouts: three different clocks, not one

The single most common bug is using one timeout for all three phases.

1. **Connect timeout** — 2–5 s. A dead endpoint should fail fast.
2. **Time-to-first-token** — 60–300 s. A reasoning model or a cold vLLM server with a big model can
   legitimately take minutes before the first byte. opencode's default here is
   `OPENAI_HEADER_TIMEOUT_DEFAULT = 300_000` (5 min).
3. **Inter-chunk (idle/stall) timeout** — 30–120 s, applied per `read()` inside the stream. This is
   the one that catches a hung generation without killing a slow-but-healthy one.

There must be **no total-request deadline** on a streamed generation, or you will kill successful
long outputs. hermes-agent makes the same distinction — it has `get_provider_request_timeout` and
`get_provider_stale_timeout` as separate functions, and it deliberately routes auxiliary calls
through a streaming path *specifically so the timeout acts per-read rather than as a total budget*.

hermes also auto-bumps timeouts for local endpoints, via `is_local_endpoint(base_url)`, which returns
true for loopback, `host.docker.internal` and friends, **any unqualified hostname (no dots — i.e.
Docker Compose service names)**, RFC-1918 ranges, link-local, and Tailscale CGNAT `100.64.0.0/10`.
That last one is there so a remote-but-trusted Ollama box on a Tailscale mesh gets local timeouts.
For Aleph, which is deployed by docker compose and will routinely be pointed at
`http://ollama:11434/v1`, the unqualified-hostname rule is directly load-bearing.

### 3.2 Retries

Aleph's current policy (`aleph_models/retry.py`) is 3 attempts, `wait_exponential(min=1, max=4)`,
retry on transport errors, timeouts, 429 and 5xx. Gaps, in order of severity:

1. **`Retry-After` is ignored.** A 429 from a real provider usually carries `Retry-After` (seconds)
   or `x-ratelimit-reset-*` headers. Backing off 1 s when the server said 47 s means three wasted
   attempts and a hard failure. The OpenAI SDK honours `Retry-After` up to 120 s; copy that,
   with a cap.
2. **No jitter.** `wait_exponential` alone synchronises retries across concurrent workers into a
   thundering herd. Use full jitter.
3. **Retrying 500 blindly is wrong for streaming.** If the stream already emitted tokens, a retry
   re-bills the whole prompt and produces a second partial answer. Retry only if nothing was emitted;
   otherwise surface the partial and let the caller decide.
4. **Not every 400 is fatal, and not every 5xx is transient.** hermes-agent's `error_classifier.py`
   is the reference: a `FailoverReason` enum of ~25 members, each mapping to one of five recovery
   actions (retry / rotate credential / fall back to another provider / compress context / abort),
   with `retryable`, `should_compress`, `should_rotate_credential`, `should_fallback` carried on the
   result so the retry loop never re-classifies. Members worth stealing wholesale:
   `context_overflow` (compress, do not fail over), `payload_too_large` (413), `image_too_large`
   (shrink and retry), `model_not_found` (fall back to a different model, do not retry),
   `content_policy_blocked` (deterministic — retrying unchanged is pure waste),
   `ssl_cert_verification` (deterministic per host — fail fast with actionable guidance rather than
   burning three retries on an identical handshake failure), `upstream_rate_limit` (the *aggregator*
   is throttled, so change model, not credential — distinct from `rate_limit`).
5. **No cooldown after exhausting a fallback chain.** hermes arms
   `_FALLBACK_EXHAUSTED_COOLDOWN_S = 5.0` after a non-rate-limit exhaustion, with a comment
   explaining why: without it, a client that resubmits immediately re-marshals the full (potentially
   80k-token) context once per provider *every turn* and can drive a constrained host into swap.

### 3.3 Idempotency

There is **no idempotency standard for LLM chat APIs.** `Idempotency-Key` is a de facto convention
(the IETF draft expired without becoming an RFC); OpenAI supports it on its Agentic Commerce
endpoints, not on chat. Stripe-style semantics — same key returns the cached response, mismatched
parameters return 409 — are not available to you here.

Aleph's existing approach is the right one and should be kept and hardened: **client-side
idempotency via a Redis-checked `idempotency_key` in `LiteLLMClient`.** Two things to add: record the
key *before* dispatch (so a crash mid-call does not produce a duplicate on restart), and store the
upstream `x-request-id` alongside it so a spend dispute can be traced to a specific upstream call.

### 3.4 Cancellation and disconnects

- **Client → server.** Aborting the HTTP request is the only mechanism. Whether the server actually
  stops generating varies: vLLM aborts on `request.is_disconnected()`, **but this is fragile** —
  vLLM issue #10087 documents that adding any `BaseHTTPMiddleware` makes `is_disconnected()` always
  return `False`, so a middleware'd deployment keeps generating (and keeps occupying GPU) after the
  client leaves. vLLM 0.27-era also exposes `POST /v1/responses/{id}/cancel` for the Responses API,
  which is the only *explicit* cancel primitive in the OpenAI-shaped surface.
- **Billing.** Assume you are billed for everything generated up to the abort, including on aborts you
  initiated. Any accounting that only writes a row on clean completion will systematically
  under-report.
- **Server → client.** A dropped connection is indistinguishable from a very slow model without an
  idle timeout. See §3.1.
- **Proxies in between.** LiteLLM, nginx, and cloud load balancers all have their own idle timeouts
  that will cut a stream mid-generation with no error frame.

### 3.5 Fallback chains

Get the *ordering* right, because the wrong order wastes money:

1. **Same model, same endpoint, retry** — for `429`, `5xx`, transport errors.
2. **Same model, different credential** — for auth/billing failures, if a credential pool exists.
3. **Same capability, different model** — for `model_not_found`, `upstream_rate_limit`, context
   overflow that compression cannot fix. This is where Aleph's `ModelProfile` capability →
   binding indirection already pays off: a fallback is "resolve `Capability.SYNTHESIS` to the next
   candidate", not "try `gpt-4o` then `claude-…`".
4. **Degrade the request** — drop `strict`, drop the tool schema's unsupported keywords, compress
   context, shrink images.
5. **Abort with a classified error** the UI can explain.

Every hop must be recorded. A `ModelCall` row for a failed attempt is as important as one for a
success — otherwise the fallback chain is invisible and unbudgeted.

---

## 4. Cost and usage accounting when the endpoint will not tell you

### 4.1 What you are actually up against

- Streaming withholds usage unless you ask (`stream_options.include_usage`), and some endpoints
  reject the ask.
- An aborted or dropped stream never delivers the usage chunk at all.
- Self-hosted endpoints (vLLM, Ollama, llama.cpp) have **no prices** — there is nothing to report.
- Prompt-cache savings are reported inconsistently and sometimes double-counted.
- The gateway's own books can silently stop (LiteLLM #32487).

Aleph's `pricing_source` field — `gateway` / `static` / `unknown` — is exactly the right primitive and
should be extended, not replaced.

### 4.2 The rule: every call writes a row, always

Make it structurally impossible for a call to complete without a `ModelCall`. The row carries not just
the numbers but **how they were obtained**:

| `usage_source` | Meaning |
|---|---|
| `reported` | Server sent `usage`. |
| `reported_partial` | Stream ended early; input tokens known from a non-streamed count, output estimated from what arrived. |
| `estimated` | No usage at all — counted client-side. |
| `unknown` | Could not even estimate (e.g. abort before any bytes). Still a row. |

Pair it with the existing `pricing_source` (`gateway` / `catalog` / `static` / `unknown`) so every
dollar figure is traceable to a method. `$0.00` must be a *stated* `unknown`, never a default.

### 4.3 Estimating when you must

- **Input tokens** can be counted client-side before dispatch. For local models, `/tokenize` exists on
  vLLM and llama.cpp; otherwise a tokenizer approximation is fine if labelled.
- **Output tokens** during a stream can be counted from the deltas you actually received. This is the
  fallback that survives an abort — count as you go, do not wait for the final chunk.
- **Never** silently substitute an estimate for a report. The label is the whole point.

### 4.4 Reconcile against the gateway, do not delegate to it

If the endpoint is LiteLLM, it keeps its own `LiteLLM_SpendLogs`. Treat that as a **second book to
reconcile against**, not the book of record: capture `x-litellm-response-cost` when present
(non-streaming only — it is a pre-flush estimate on streams), capture `x-request-id`, and run a
periodic reconciliation that reports drift. Drift is the signal that a whole class of calls is going
unrecorded on one side or the other — which is precisely the bug LiteLLM shipped.

### 4.5 Prices without a hardcoded price list

The constraint is "no hardcoded price list", and Aleph honours it today by asking the gateway and
falling back to an operator hints file. There is a better middle rung available, and it is the single
biggest quick win in this document:

**Two public, machine-readable, continuously-updated catalogs exist and both were live when I
fetched them on 2026-08-19:**

- **`https://models.dev/api.json`** — 4.0 MB, **192 providers, 6,838 models**. Per model:
  `id, name, description, family, attachment (files), reasoning, tool_call, structured_output,
  temperature, knowledge (training cutoff), release_date, last_updated, open_weights,
  modalities{input[],output[]}, limit{context,output}, cost{input,output,cache_read,cache_write}`.
  Provider records carry `env` (which env vars hold the key), `npm` (which SDK speaks to it), and
  `doc`. This is what **opencode** uses; it fetches, caches to disk under a flock, and refreshes on a
  schedule.
- **`model_prices_and_context_window.json`** (LiteLLM's, on GitHub raw) — 1.8 MB, **3,055 entries**.
  Per model: `mode`, `max_input_tokens`, `max_output_tokens`, per-token input/output cost plus
  `cache_read_input_token_cost`, batch and priority tier rates, and capability booleans
  `supports_function_calling`, `supports_parallel_function_calling`, `supports_tool_choice`,
  `supports_response_schema`, `supports_prompt_caching`, `supports_vision`, `supports_pdf_input`,
  `supports_system_messages`.

A **fetched, cached, dated snapshot** is not a hardcoded list. It is discovery from a third source,
and it slots cleanly into Aleph's existing precedence chain as a new tier between gateway and hints:

```
gateway /model/info   →  pricing_source="gateway"    (reported by the deployment; wins)
public catalog        →  pricing_source="catalog"    (fetched; carries catalog name + fetch date)
operator hints file   →  pricing_source="static"     (asserted by the operator)
nothing               →  pricing_source="unknown"    (loud, never $0)
```

The rule Aleph's `hints.py` already states — *capability facts are properties of the model and safe to
state; rates are properties of the deployment and only an estimate* — applies to the catalog tier
verbatim, and is the correct guard against a self-hosted vLLM inheriting OpenAI's prices because the
model id happens to match.

---

## 5. Auth patterns

### 5.1 The layers

1. **The endpoint credential** — the bearer token Aleph presents to the base URL. Today
   `INSIGHTS_LITELLM_API_KEY`, read from a container environment variable.
2. **Per-project scoping** — which project's spend a call belongs to.
3. **The caller's identity** — which human or agent initiated it.

Aleph currently conflates 1 and 2: one process-wide key, project attribution done in Aleph's own
ledger. That is workable and honest, but it means the gateway's own budgets, rate limits and spend
logs cannot see projects at all.

### 5.2 Virtual keys

LiteLLM's virtual keys are the mechanism (docs note "available in v1.95.0 and later"; current is
1.97.0). A virtual key is minted by the proxy, carries `max_budget`, `model_max_budget`, allowed
models via **model access groups**, TPM/RPM limits, and metadata/tags that flow into spend logs.
`POST /key/generate` mints one.

The upgrade path for Aleph: mint **one virtual key per project** at project creation, store it
encrypted (Aleph already has `ConnectorCredential` with encryption — reuse that machinery), and
present it on that project's calls. You then get per-project budget enforcement *at the gateway*, a
kill switch per project, and spend logs that agree with Aleph's ledger without a join. Gate the whole
thing behind a capability check — the proxy must expose `/key/generate` and Aleph must hold a key
allowed to call it — and degrade to the single shared key when it does not.

### 5.3 Header pass-through

LiteLLM supports client-side credential pass-through (documented for the Vertex flow: specific,
default, or client-supplied credentials). Useful when a human's own provider key should be billed
rather than the deployment's. **Treat pass-through as high-risk**: it means a caller-supplied header
reaches an upstream provider. If Aleph adopts it, allowlist exactly which headers may pass, never
log them, and never let an *agent* originate one — an agent that can set an outbound auth header is
an agent that can exfiltrate.

### 5.4 Keeping credentials out of container environment variables

This is a stated goal and Aleph currently violates it (`INSIGHTS_LITELLM_API_KEY` is compose env).
The problem with env vars is real: they are visible to every process in the container, appear in
`docker inspect`, leak into crash dumps and log lines, and get committed in `.env` files.

The compose-native answer is **file-mounted secrets**. Compose supports a top-level `secrets:` block;
files land read-only at `/run/secrets/<name>`, scoped to services that request them. Compose does not
*encrypt* at rest (that is Swarm) — but it removes the secret from the process environment and from
`docker inspect`, which is the bulk of the exposure.

The conventional pattern, and the one to adopt:

- Support `ALEPH_GATEWAY_API_KEY_FILE` alongside `ALEPH_GATEWAY_API_KEY`, with the file taking
  precedence. This `_FILE` convention is what Postgres, Redis and most official images use, so it
  needs no explanation to an operator.
- Read the file **at use time, not at import time**, so a rotated secret is picked up without a
  restart.
- Redact by construction: hold the key in a wrapper type whose `__repr__`/`__str__` returns
  `***`, so it cannot land in a structlog field or a traceback by accident.
- For anything beyond the boot credential — per-project virtual keys, per-connector tokens — the
  database with envelope encryption is the right home, which is what `ConnectorCredential` already is.
- Never let a plugin read the raw credential. In a kernel with scoped capability access, the LLM
  capability should hand out a *bound client*, not a key. That is the strongest argument for keeping
  a single transport chokepoint: it is also the credential boundary.

---

## 6. Embeddings — the operational hazard, in detail

### 6.1 Why embeddings are worse than chat

A wrong chat response is one bad answer. A wrong embedding **silently corrupts a persistent index**
that everything else reads, and the damage is only visible as degraded retrieval quality — which
looks like "the model got dumber", not "the operator changed a setting". Aleph's whole belief layer
sits downstream of retrieval, and `docs/acceptance.md` §B pins recall@1 at 0.91 as the bar. A
half-migrated embedding index fails that bar with no error anywhere.

### 6.2 What actually differs across endpoints

- **Output dimension is not discoverable before you pay.** `/v1/models` returns ids. `/model/info`
  (admin-gated, usually 403 for an application key) may carry `output_vector_size`, but you cannot
  count on it. The only universally reliable way to learn an embedder's dimension is **to embed one
  short string and measure `len(vector)`** — which is exactly what Aleph's `is_known_embedding_model`
  docstring already describes as the fallback.
- **`dimensions` (Matryoshka truncation).** OpenAI's `text-embedding-3-*` were trained so a prefix of
  the vector is still a usable embedding; passing `dimensions: 256` returns a shortened, **already
  L2-normalized** vector. Ollama supports a dimension parameter. vLLM support is model-dependent.
  llama.cpp and TGI: not supported. **A server that ignores `dimensions` returns the full-width
  vector** — another category-3 silent divergence, and one that will blow up on insert into a fixed
  pgvector column (which at least fails loudly) or, worse, succeed at a width you did not intend.
- **Normalization.** Some servers return unit-norm vectors, some do not. If half your index is
  normalized and half is not, cosine similarity still returns numbers — just wrong ones. Never assume;
  normalize client-side before storing, always, and record that you did.
- **Batch limits.** No standard. OpenAI accepts large arrays; some servers cap at 32/64/256 inputs or
  on total tokens; some cap on request body bytes. Jina documents no limit. The only safe policy is
  an adaptive batcher: start conservative, halve on 400/413, and remember the working size per endpoint.
- **`encoding_format: "base64"`** returns packed float32 instead of a JSON array of floats — roughly
  4× less wire traffic and much less JSON parsing for large batches. Supported by OpenAI and several
  others; **not universal**, so it must be negotiated (try once, fall back to `float`) and never
  assumed.
- **Token-array input.** OpenAI accepts pre-tokenized input; Ollama explicitly does not.
- **Truncation behaviour.** Over-long input is silently truncated by some servers and 400'd by
  others. Silent truncation means an embedding that represents the first half of a chunk. Measure
  input length client-side.

### 6.3 What breaks when the operator swaps the embedding model — and what Aleph does today

Aleph's current state, read from the tree:

- `packages/aleph-rks/src/aleph_rks/models.py:34` — `EMBEDDING_DIM = 1024`, a module constant.
- Same file, line 155 — `mapped_column(Vector(EMBEDDING_DIM), nullable=False)`. **The column width is
  compiled into the schema.**
- `packages/aleph-rks/src/aleph_rks/embedding.py` — a hardcoded `KNOWN_EMBEDDING_DIMS` dict of nine
  model names (`titan-embed-v2`, `text-embedding-3-small`, `bge-m3`, …) used to reject a
  dimension-mismatched embedder *before* a billed call.
- `retrieval.py` — a genuinely good guard on the re-embed sweep: it reads `len(chunks[0].embedding)`,
  compares against the target model's known dimension, and on mismatch logs
  `rks.reembed.dim_mismatch_skipped`, increments `dim_blocked`, and **deliberately leaves
  `embedder_model` at its old value** so the row stays in the stale set and is re-detected (and
  re-skipped, still unbilled) on the next sweep.

The guard logic is the right shape — reject before paying, leave a durable queryable mark. The
problems are:

1. **`KNOWN_EMBEDDING_DIMS` is a hardcoded model list**, which is the exact thing the project
   constraint forbids, and it is already stale-by-construction: an operator pointing at
   `Qwen3-Embedding-8B` (4096) or `nomic-embed-text` (768) or `gte-modernbert-base` (768) hits the
   "unknown" path and discovers the mismatch by paying for it.
2. **`EMBEDDING_DIM` is a compile-time constant**, so "swap the embedder" is a schema migration, not
   a configuration change. There is no supported path from 1024 to 1536.
3. **There is no per-row record of which model produced a vector at the dimension level.** There is an
   `embedder_model` field (good), but no `embedding_dim` and no notion of an *active* embedding
   version that queries filter on.

### 6.4 The correct design

The consensus best practice, and it matches what Aleph's own guard is groping toward:

- **Store the embedding model and its dimension on every vector row.** `embedder_model` exists; add
  `embedding_dim` (and ideally a `normalized` flag).
- **Make dimension a runtime fact, discovered by probe, not a constant.** On first bind of an
  embedding capability, embed the single token `"a"`, measure the length, and record it on the
  binding. One token of spend buys certainty for every model, including ones nobody has heard of.
  Delete `KNOWN_EMBEDDING_DIMS` entirely — a fetched catalog (models.dev / LiteLLM) can *pre-warn*,
  but the probe decides.
- **Never write a vector whose dimension disagrees with the column.** Aleph already does this.
- **Treat an embedding model change as a migration with a cutover, not a config edit.** The standard
  shape: add a new column or a new table partition at the new width, backfill with
  `CREATE INDEX CONCURRENTLY`, run both indexes in parallel while you compare recall on the eval set
  Aleph already has, then flip the "active embedding version" and keep the old one as fallback until
  validated. `REINDEX CONCURRENTLY` avoids table locks.
- **Filter queries by active embedding version.** Without this, a partially-backfilled index returns
  a mix of old-space and new-space vectors, and cosine similarity across two embedding spaces is
  meaningless — it does not error, it just returns garbage in ranked order.
- **Gate the swap on the eval.** Aleph is unusually well-placed here: it has a 45-pair labelled
  retrieval set and a runner (`python -m aleph_evals.retrieval_eval`). Make "recall@1 on the new
  embedder ≥ the old" a required check before cutover. That converts the single scariest operator
  action into a measured one.
- **Refuse to start with a mismatch.** The kernel capability probe for embeddings should embed one
  token and compare against the stored column width at boot. `CLAUDE.md` already says a capability
  that cannot answer a live query must not come up — this is that rule applied to the exact place it
  matters most.

---

## 7. Model discovery without a hardcoded list

Aleph's `discovery.py` docstring is one of the best pieces of writing in the repo and its diagnosis is
correct: `/model/info` is the honest source but is admin-gated, so an application virtual key gets
403 and falls back to `/v1/models`, which returns ids and nothing else. The lesson it records — a
Bedrock-backed deployment where not one model name matched and rates were ~3× wrong, producing a
plausible $0.00 dashboard — is the whole argument for probing.

The five-tier chain that follows from everything above:

1. **`GET /model/info`** (LiteLLM admin) — modes, context windows, capability flags, exact rates.
   `source = gateway`. Best when available.
2. **`GET /v1/models`** — universal, ids only. Establishes *existence*.
3. **Public catalog** — models.dev (6,838 models) and/or LiteLLM's price JSON (3,055). Fetched,
   cached with a fetch date, matched on normalized model id. Fills capability flags and *estimated*
   rates. `source = catalog`. Never overrides tier 1.
4. **Operator hints** — `ALEPH_MODEL_HINTS_PATH`. `source = static`. Never overrides 1 or 3 for the
   same field.
5. **Live probe** — the arbiter. Aleph's `probe_model` already exists. Extend it from "can I reach
   this?" to a small capability suite, run once per (endpoint, model) and cached:
   - a 1-token chat → reachability, and whether `max_tokens` or `max_completion_tokens` is accepted;
   - a 1-token streamed chat with `stream_options.include_usage` → does usage actually arrive;
   - a trivial tool call with `tool_choice` forced → is forcing honoured;
   - a trivial `json_schema` request → is the schema honoured;
   - for embedders, embed `"a"` → dimension and norm.

The output is the `EndpointProfile` from §2.3, with each field tagged by which tier established it.
Rule from `hints.py`, generalised: **capability facts are properties of the model and safe to
inherit from a catalog; rates are properties of the deployment and must be labelled as estimates
until a bill confirms them.** A self-hosted vLLM serving a model whose id matches an OpenAI one must
not inherit OpenAI's prices — match on `(endpoint_identity, model_id)`, and when the endpoint looks
local (see `is_local_endpoint`, §3.1), suppress catalog pricing entirely and mark it `unknown`.

---

## 8. On the performance worry

The owner's concern that a plugin architecture will be slow does not apply meaningfully to this
subsystem, and it is worth being precise about why.

- **The dominant cost is the network.** A chat call is 0.3–120 s of remote compute. Ten microseconds
  of capability lookup is not measurable against that. Aleph's own `LiteLLMClient` already wraps every
  call in an OTEL span, a Redis idempotency check and two DB writes — all far more expensive than a
  dict lookup on a resolved `EndpointProfile`.
- **Resolve once, cache, reuse.** Compat detection, model discovery and probes are per-endpoint,
  not per-request. prime-agent resolves compat per model and merges overrides once; opencode caches
  the models.dev catalog on disk under a flock and lazily imports each provider module only when
  first used.
- **Where real latency hides**, in descending order: (1) creating a new HTTP client (and therefore a
  new TLS handshake) per call instead of reusing a pooled one — this is worth 50–200 ms per request
  and is a genuinely common bug; (2) a probe on the hot path instead of at bind time; (3) blocking
  the event loop with synchronous JSON parsing of a 4 MB catalog; (4) unbatched embedding calls.
- **The plugin boundary that could cost you** is a per-token crossing — e.g. routing every SSE delta
  through a dynamically-loaded transform. Design so plugins participate at *request* and *response*
  granularity, and stream deltas straight through.

opencode is the existence proof: it dynamic-imports provider modules by npm package name, can install
unknown providers from npm at runtime, and is a latency-sensitive interactive TUI. Prime-agent's
`registerApiProvider` / `unregisterApiProviders(sourceId)` registry is a runtime-mutable protocol table
in a shipping agent. Runtime-pluggable model transport is demonstrated, not theoretical.

---

## What Aleph should do

1. **Keep the hand-rolled `httpx` transport as the single chokepoint.** No reference system uses the
   LiteLLM SDK in-process, and adopting the official OpenAI SDK today means an `httpx2` migration
   across the whole Python workspace (SDK v3.0.0, 2026-08-12). Steal the SDK's *policies* instead:
   `Retry-After` honouring, granular connect/read/write timeouts, `x-request-id` capture.
2. **Introduce an explicit, probed `EndpointProfile`** — prime-agent's
   `ResolvedOpenAICompletionsCompat` plus a probe step. Resolve it once per (endpoint, model),
   cache it, and make the request builder read from it instead of assuming. Ship its field list from
   §2.3; every field there exists because something broke in production.
3. **Probe before binding, and make the probe a capability suite**, not a ping: forced tool call,
   `json_schema` echo, streamed usage, and — for embedders — a one-token dimension measurement.
   Aleph already probes for reachability; extend it. A capability that cannot demonstrate what it
   claims must not come up.
4. **Add a fetched public catalog as a discovery tier between the gateway and the hints file.**
   `https://models.dev/api.json` (192 providers, 6,838 models, verified live 2026-08-19) and/or
   LiteLLM's `model_prices_and_context_window.json` (3,055 entries). Cache with a fetch date, tag
   rows `pricing_source="catalog"`, never override the gateway, and suppress catalog *pricing*
   entirely for local endpoints.
5. **Delete `KNOWN_EMBEDDING_DIMS` and make `EMBEDDING_DIM` a runtime fact.** Store
   `embedder_model` + `embedding_dim` (+ a normalized flag) on every vector row, discover dimension
   by embedding one token at bind time, and filter queries by an active embedding version. Gate any
   embedder swap on the existing 45-pair retrieval eval.
6. **Close the usage hole structurally.** Every call writes a `ModelCall`, always, carrying a
   `usage_source` of `reported` / `reported_partial` / `estimated` / `unknown` beside the existing
   `pricing_source`. Count output tokens from deltas as they arrive so an aborted stream still yields
   a number. `$0.00` must be a stated `unknown`, never a default.
7. **Adopt three separate timeout clocks** — connect (2–5 s), time-to-first-token (60–300 s), and a
   per-chunk idle timeout (30–120 s) — and no total deadline on a streamed generation. Copy opencode's
   `wrapSSE` pattern: race each `reader.read()` against a timer, abort the controller on expiry.
   Auto-relax for local endpoints using a `is_local_endpoint` check that treats unqualified hostnames
   (Docker Compose service names) as local.
8. **Replace the 33-line retry policy with a classified error taxonomy** modelled on hermes-agent's
   `error_classifier.py`: a reason enum, four recovery-hint booleans, `Retry-After` honouring with a
   cap, full jitter, no blind retry of a stream that already emitted tokens, and a short cooldown
   after a fallback chain is exhausted.
9. **Mint one LiteLLM virtual key per project** where the proxy supports it, store it with the
   existing `ConnectorCredential` encryption, and degrade cleanly to the shared key when it does not.
10. **Move the gateway credential out of the container environment.** Support
    `ALEPH_GATEWAY_API_KEY_FILE` (compose `secrets:` → `/run/secrets/...`), read at use time so
    rotation needs no restart, and wrap it in a type whose repr is `***`. Plugins receive a bound
    client, never a key.
11. **Model reranking as a separate optional capability with its own base URL**, speaking Cohere's
    `/v1/rerank` shape (vLLM serves it Cohere- and Jina-compatible; Ollama/llama.cpp/TGI do not serve
    it at all).
12. **Make the protocol (`chat.completions` vs `responses`) a property of the resolved binding**, not
    a global switch — the same deployment will have models reachable only over one.

## What Aleph should avoid

1. **Do not treat "OpenAI-compatible" as a contract.** The dangerous cases return HTTP 200: vLLM
   accepts `strict: true` and ignores it; Ollama accepts `tool_choice` and ignores it. Assume a
   capability is absent until probed.
2. **Do not depend on LiteLLM proxy features being present.** The moment an operator points Aleph at
   raw Ollama, the proxy's fallbacks, budgets, virtual keys and spend logs vanish. Use them as
   optimisations; never as the mechanism.
3. **Do not make the gateway the book of record for cost.** LiteLLM currently fails to write a spend
   row for streaming `/v1/responses` (issue #32487), and `x-litellm-response-cost` is a pre-flush
   estimate on streams (#12689). Keep your own ledger and reconcile.
4. **Do not add the LiteLLM SDK as an in-process dependency.** It duplicates the proxy you already
   run, drags a large transitive tree into the kernel's composition root, and none of opencode,
   prime-agent or hermes-agent does it.
5. **Do not adopt the official OpenAI Python SDK without budgeting the `httpx2` migration.** v3.0.0
   dropped `httpx`; Aleph pins `httpx==0.28.1` in at least three packages. And do not pin it loosely —
   it shipped 2.54.0 → 3.3.0 in six days.
6. **Do not use one timeout for everything.** A single 60 s deadline kills legitimate long
   generations and fails to catch a hung stream. Do not put a total deadline on a streamed call.
7. **Do not retry a stream that already emitted tokens.** You pay for the prompt twice and get two
   partial answers.
8. **Do not hardcode any model list, price list, or embedding dimension.** `KNOWN_EMBEDDING_DIMS` and
   `EMBEDDING_DIM = 1024` are the two live violations. A *fetched, dated, cached* catalog is not a
   hardcoded list; a dict in a `.py` file is.
9. **Do not assume `usage` will arrive.** It is withheld on streams unless requested, rejected outright
   by some endpoints (Gemini's native REST), and never delivered at all on an abort.
10. **Do not let a rate be silently zero.** An unpriced call must be a loud `unknown`, not a $0 row —
    this is the failure that made a mismatched price table invisible for months.
11. **Do not change the embedding model as a configuration edit.** Without a versioned index and a
    validated cutover, you get an index that mixes two embedding spaces, returns confidently ranked
    nonsense, and raises no error anywhere.
12. **Do not let plugins hold raw credentials or set outbound auth headers.** The transport chokepoint
    is also the credential boundary; an agent that can set an `Authorization` header can exfiltrate.
13. **Do not put a plugin boundary on the per-token path.** Plugins act at request/response
    granularity; stream deltas pass straight through.

---

## Sources

Version data verified directly against PyPI JSON API, the npm registry, and the GitHub releases API
on 2026-08-19; catalog sizes verified by fetching the files.

- OpenAI Python SDK changelog — https://github.com/openai/openai-python/blob/main/CHANGELOG.md
- OpenAI deprecations — https://developers.openai.com/api/docs/deprecations
- Codex dropping chat/completions — https://github.com/openai/codex/discussions/7782
- vLLM OpenAI-compatible server — https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/
- vLLM tool calling (incl. the `strict` note) — https://docs.vllm.ai/en/stable/features/tool_calling
- vLLM structured outputs backends — https://docs.vllm.ai/en/stable/api/vllm/v1/structured_output
- vLLM client-disconnect bug with middleware — https://github.com/vllm-project/vllm/issues/10087
- Ollama OpenAI compatibility — https://docs.ollama.com/api/openai-compatibility
- LiteLLM model discovery — https://docs.litellm.ai/docs/proxy/model_discovery
- LiteLLM virtual keys — https://docs.litellm.ai/docs/proxy/virtual_keys
- LiteLLM reliability (fallbacks/retries/cooldowns) — https://docs.litellm.ai/docs/proxy/reliability
- LiteLLM response headers — https://docs.litellm.ai/docs/proxy/response_headers
- LiteLLM streaming `/v1/responses` uncharged — https://github.com/BerriAI/litellm/issues/32487
- LiteLLM cost header on streams — https://github.com/BerriAI/litellm/issues/12689
- AI SDK openai-compatible provider — https://ai-sdk.dev/providers/openai-compatible-providers
- models.dev — https://models.dev/ and https://models.dev/api.json
- Docker Compose secrets — https://docs.docker.com/ai/docker-agent/guides/secrets/
- Matryoshka / `dimensions` — https://supabase.com/blog/matryoshka-embeddings
- pgvector model-swap practice — https://dbadataverse.com/tech/postgresql/2026/05/pgvector-gotchas-dimension-mismatch-casting-errors-and-alter-table-solved-2026

Local codebases read (all MIT, read as blueprints, not dependencies):
`~/Documents/code/inspiration/opencode`, `~/Documents/code/inspiration/prime-agent`
(`packages/ai` = `@earendil-works/pi-ai` 0.7.3), `~/Documents/code/inspiration/hermes-agent` 0.20.4.
Aleph read at `/Users/jpmullins/Documents/code/aleph` (`packages/aleph-models`, `packages/aleph-rks`).
