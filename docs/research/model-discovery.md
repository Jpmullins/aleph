# Model discovery and capability-driven configuration

Research current as of **19 August 2026**. Every version number and endpoint shape below was
checked live on that date, or read from source on the named branch/tag. Where I could not verify
something, it says **unverified**.

---

## In one paragraph

Aleph's rule — ship no model list and no price list, ask the endpoint instead — is the right rule,
and Aleph's existing implementation gets the hard part right: it prefers what the gateway reports,
it labels asserted numbers differently from reported ones, and it refuses to bind a model it cannot
justify. Three things have changed since it was written. First, **the specific problem it was
designed around largely went away**: LiteLLM v1.96.0 (released 10 Aug 2026) added `/model/info` to
the route list that an application's restricted virtual key is allowed to call, so the 403 that
forced Aleph onto the ids-only fallback path no longer happens on a current gateway. Second, **the
ids-only fallback is now leaving real data on the floor**: several gateways return context windows,
pricing, modalities and supported parameters *inside the standard `/v1/models` envelope*, and
Aleph's parser reads only the `id` field and discards the rest. Third, **the hand-maintained hints
file has a much better replacement**: `models.dev` (192 providers, 6,838 models, live) and LiteLLM's
own `model_prices_and_context_window.json` (3,055 models, 151 metadata fields) are both
runtime-fetchable community catalogs that give you exactly what a bare id cannot, without
committing a table to the repo. Beyond that, the deepest structural criticism is this: Aleph
eliminated "unknown price silently means $0" and then reintroduced the identical mistake one field
over — **"unknown capability silently means False"**. A model discovered from `/v1/models` gets
`supports_function_calling=False`, which is indistinguishable from a model that genuinely cannot
call tools, and that is what makes an entire gateway look incapable when it is merely undescribed.

---

## The words

Defined once, used throughout.

- **Endpoint** — the HTTP address Aleph talks to. Always OpenAI-compatible. May be a *gateway*
  (LiteLLM, OpenRouter, Vercel AI Gateway) that fans out to many upstream providers, or a *server*
  (Ollama, vLLM) running models on one machine.
- **Discovery** — asking the endpoint what models it serves.
- **Metadata** — facts *about* a model: how much text it can read (context window), how much it can
  write (max output), whether it can look at images (vision), whether it can call tools, what it
  costs.
- **Capability** — in Aleph's sense, a *job*: "summarize", "embed", "judge", "extract". Call sites
  ask for a job; a resolver picks the model. This is the good design and everything below assumes it.
- **Binding** — the recorded decision that capability X uses model Y.
- **Probe** — actually calling a model to find out something the metadata did not say, or said
  wrongly.
- **Virtual key** — a scoped API key issued by a gateway to an application, as opposed to the
  operator's admin key. The whole "restricted-key problem" is about what a virtual key may read.
- **Catalog** — a third-party database of model metadata, fetched over the network, not committed
  to your repo.
- **Provenance** — the record of *where a fact came from*: reported by the gateway, read from a
  catalog, asserted by an operator, or established by a probe. Aleph already tracks this for price.
  It should track it for every field.

---

## Part 1 — What each endpoint actually exposes

### The summary table

| Endpoint | Auth | Ids | Context window | Max output | Modality | Tool support | Price | Notes |
|---|---|---|---|---|---|---|---|---|
| **OpenAI `/v1/models`** | key | yes | no | no | no | no | no | Unchanged. Feature request open, not implemented. |
| **LiteLLM `/v1/models`** | any key | yes | no | no | no | no | no | Bare OpenAI shape. Issue #21855 asked to enrich it; **closed as not planned**. |
| **LiteLLM `/model/info`** | admin key **or LLM-API key from v1.96.0** | yes | yes | yes | yes | yes | yes (exact, incl. cache rates) | The good one. See Part 2. |
| **LiteLLM `/model_group/info`** | info/admin key only | yes | yes | yes | yes | yes | partial | Not in `llm_api_routes`. Still gated. |
| **LiteLLM `/public/litellm_model_cost_map`** | **none** | — | yes | yes | yes | yes | yes | Public route. Returns the *upstream* catalog, not your gateway's aliases. |
| **LiteLLM `/public/model_hub`** | key | yes | yes | yes | yes | yes | yes | Opt-in: admin must publish model groups. |
| **OpenRouter `/api/v1/models`** | **none** | yes | yes | yes | yes (5 input, 3 output kinds) | yes (`supported_parameters`) | yes (10 price dimensions) | Richest metadata of anything shipping. |
| **Vercel AI Gateway `/v1/models`** | **none** | yes | yes | yes | yes | yes | yes | Rich metadata *in the standard `/v1/models` envelope*. |
| **Ollama `/api/tags`** | none (local) | yes | no | no | no | no | n/a | Ids, size, digest, quantization, modified time. |
| **Ollama `/api/show`** | none (local) | — | yes (trained) | no | yes | yes | n/a | `capabilities` array. One call per model. |
| **Ollama `/v1/models`** | none | yes | no | no | no | no | n/a | Compat shim. Useless for metadata. |
| **vLLM `/v1/models`** | optional | yes | **yes (`max_model_len`)** | no | no | no | n/a | Also `root` (the HF repo id) and `parent` (LoRA base). |
| **Bedrock `ListFoundationModels`** | AWS SigV4 | yes | no | no | yes (TEXT/IMAGE/EMBEDDING) | no | no | Plus `inferenceTypesSupported`, `modelLifecycle`, `responseStreamingSupported`. |

### Per-endpoint detail

**OpenAI, and every plain OpenAI-compatible server.** `{"id", "object", "created", "owned_by"}`.
Nothing else, still, in August 2026. There is a long-standing community request to expose
capabilities in `/v1/models`; it has not been implemented. Treat "ids only" as the floor case, not
the exception.

**LiteLLM `/model/info`.** One row per configured model. `model_name` is the *alias* the gateway
serves (what you put in a request); `model_info` carries `mode`
(`chat`/`embedding`/`rerank`/`image_generation`/…), `max_input_tokens`, `max_output_tokens`,
`input_cost_per_token`, `output_cost_per_token`, `cache_read_input_token_cost`,
`cache_creation_input_token_cost`, and a wide set of `supports_*` booleans. `litellm_params.model`
carries the *upstream* id (`bedrock/anthropic.claude-…`), which is how you learn the real provider.
This is the endpoint Aleph already prefers, correctly.

**LiteLLM `/model_group/info`.** Similar data, aggregated per model group with a `providers` array
and per-group flags (`supports_pdf_input`, `supports_audio_input`, `supports_computer_use`, …). It
sits in LiteLLM's `info_routes`, **not** in `llm_api_routes`, so a restricted virtual key still
cannot call it even on the newest build. It is not a useful fallback for an application. Aleph's
docstring dismisses it for the wrong reason (missing cache rates); the real reason is the permission.

**LiteLLM `/public/litellm_model_cost_map`.** Genuinely public — it is listed in LiteLLM's
`public_routes` frozenset on `main`, meaning no credential at all. It returns LiteLLM's bundled
model catalog: I fetched the same file from source and it has **3,055 entries and 151 distinct
metadata fields**, including `mode`, `max_input_tokens`, `max_output_tokens`, ~39 `supports_*`
flags, tiered pricing (above-200k, flex, priority, batch), `output_vector_size` for embedders (60 of
124 embedding entries carry it), `supported_endpoints`, `supported_modalities`, `rpm`, and
`deprecation_date` (335 entries carry one). **Caveat that matters:** it is keyed by *canonical*
model names, not by your gateway's aliases, so using it requires a join. See Part 3.

**OpenRouter `/api/v1/models`.** Unauthenticated. I fetched it live: **415 models**. Per model:
`id`, `canonical_slug`, `hugging_face_id`, `name`, `description`, `created`, `context_length`,
`architecture{modality, input_modalities[text|image|video|audio|file],
output_modalities[text|image|audio], tokenizer, instruct_type}`, `pricing` with up to ten separate
rate dimensions (`prompt`, `completion`, `image`, `image_output`, `audio`, `audio_output`,
`web_search`, `internal_reasoning`, `input_cache_read`, `input_cache_write`, `input_cache_write_1h`),
`top_provider{context_length, max_completion_tokens, is_moderated}`, `supported_parameters` (a
26-term vocabulary including `tools`, `tool_choice`, `parallel_tool_calls`, `response_format`,
`structured_outputs`, `reasoning`, `reasoning_effort`, `verbosity`, `web_search_options`),
`default_parameters`, `knowledge_cutoff`, `expiration_date`, `reasoning{mandatory, default_enabled,
supported_efforts, default_effort}`, and — new and interesting — `benchmarks` with
`artificial_analysis{intelligence_index, coding_index, agentic_index}` and Design Arena Elo, present
on 228 of 415 models.

It also supports **server-side capability filtering**: `?supported_parameters=tools` returns 334 of
the 415. That is the closest thing to a standard "query models by capability" API that exists
anywhere, and it exists at exactly one vendor.

The `/api/v1/models/{author}/{slug}/endpoints` sub-resource is the other half: per *provider*
serving that model, it gives `quantization` (`fp8`, `bf16`, …), `context_length`,
`max_completion_tokens`, `max_prompt_tokens`, per-provider `pricing`, `supports_implicit_caching`,
`status`, and rolling `uptime_last_5m / 30m / 1d`. That is operational data nothing else publishes.

**Vercel AI Gateway `/v1/models`.** Unauthenticated, **348 models** live. This is the important
structural example: it uses the *ordinary OpenAI `/v1/models` envelope* and just adds fields —
`context_window`, `max_tokens`, `type` (`language`/`embedding`/…), `modalities{input,output}`,
`supported_parameters`, `pricing{input,output}`, `tags` (`reasoning`, `tool-use`),
`reasoning_options`, `knowledge`, `released`, plus governance fields `zdr` (zero data retention) and
`no_training`. A client that parses only `id` from `/v1/models` throws all of that away. **Aleph is
that client today.**

**Ollama.** `/api/tags` lists what is pulled locally: `name`, `model`, `modified_at`, `size`,
`digest`, and `details{format, family, families, parameter_size, quantization_level}`. It carries no
context window and no capabilities. `/api/show` (POST, one call per model) is where the real data
is: a `capabilities` array — values include `completion`, `vision`, `tools`, `thinking`, `embedding`
— plus a `model_info` map of GGUF keys where `<architecture>.context_length` is the trained window,
`<architecture>.embedding_length` the vector width. Ollama's own `/v1/models` compat shim adds
nothing.

Two traps specific to Ollama. (1) **Trained context ≠ served context.** `gemma4.context_length`
might be 131,072 while the server actually runs a much smaller window; Ollama's docs are internally
inconsistent about the default (the FAQ says 4096, the Modelfile reference says `num_ctx` defaults
to 2048, and the context-length page says it is chosen from available VRAM — 4k under 24 GiB, 32k
from 24–48 GiB, 256k above). `OLLAMA_CONTEXT_LENGTH` sets the server default; a request's `num_ctx`
overrides it. Believing the GGUF number will silently truncate your prompts. (2) `/api/show` is
**N+1**: one POST per model, so a 40-model machine means 40 round trips. Fan out with a bound and
cache.

**vLLM.** The `ModelCard` on `main` is `{id, object, created, owned_by, root, parent, max_model_len,
permission[]}`. `max_model_len` is authoritative — it is the server's *actual* configured window,
not a guess. `root` is the model path/HF repo id, which is the join key into any catalog. `parent`
is non-null for LoRA adapters and names the base model. No capability flags, no pricing. vLLM has
one optional extra: `/tokenizer_info`, gated behind `--enable-tokenizer-info-endpoint`, which
exposes the chat template — from which tool-calling support can be inferred, though that inference
is fragile. Note the flag is off by default, so do not depend on it.

**Bedrock.** `ListFoundationModels` returns `modelSummaries[]` with `modelId`, `modelArn`,
`modelName`, `providerName`, `inputModalities` / `outputModalities` (`TEXT`|`IMAGE`|`EMBEDDING`),
`responseStreamingSupported`, `customizationsSupported`, `inferenceTypesSupported`, and
`modelLifecycle`. No context window, no pricing, no tool-calling flag. `inferenceTypesSupported`
matters operationally: a value of `["INFERENCE_PROFILE"]` means the bare `modelId` will fail on
invocation and you must use an inference-profile ARN — which is exactly the failure Aleph's
`probe_model` docstring already describes from the field. `ListInferenceProfiles` enumerates those.
Pricing is not in the Bedrock control plane at all; it is in the AWS Price List API, and a
deployment can have negotiated rates that differ from it.

---

## Part 2 — The restricted-key problem, and the news

The problem as Aleph documents it: a LiteLLM virtual key issued to an application is typically
created with key type "LLM API", which sets `allowed_routes: ["llm_api_routes"]`, and `/model/info`
was not in that list. The gateway answers:

```
403 "Virtual key is not allowed to call this route.
     Only allowed to call routes: ['llm_api_routes']"
```

**This is now largely historical.** Reading `litellm/proxy/_types.py` across tags:

```
v1.91.0 … v1.95.1   model_info_routes absent
v1.96.0             model_info_routes = ["/model/info", "/v1/model/info"]
                    llm_api_routes = openai_routes + … + model_info_routes
```

LiteLLM **v1.96.0** was released **2026-08-10**. Latest stable at time of writing is **v1.97.0**
(2026-08-16), with `v1.98.0-rc.1` (2026-08-16) and `v1.99.0-dev.1` (2026-08-19) in flight. So on any
gateway running v1.96.0 or newer, a restricted application key **can** read `/model/info` and gets
modes, windows, capability flags and exact rates.

Two honest caveats:

1. The tracked GitHub issues (#20581 "/model/info requires Default key", #21855 "expose model_info
   via /v1/models") are both **closed as not planned**. The issue tracker says no; the code on
   `main` says yes. I trust the code — I read the route lists directly across six tags and bisected
   the change to a specific release. But it means the behaviour is not something the maintainers
   have committed to in writing, so a future refactor could take it away. Detect it, do not assume it.
2. `/model_group/info` was **not** added to `llm_api_routes` and remains unreachable to a restricted
   key.

### The right degradation ladder

Aleph's ladder is two rungs: `/model/info` → `/v1/models`. It should be five, tried in order, each
one recording its own provenance:

1. **`/model/info`** (LiteLLM ≥ 1.96, or an admin key). Best case. Label `gateway`.
2. **`/v1/models`, parsed for extensions.** Read `id`, and *also* `context_window`,
   `context_length`, `max_tokens`, `max_output_tokens`, `max_model_len`, `modalities`, `type`,
   `pricing`, `supported_parameters`, `tags`, `root`, `parent`, `name`, `display_name`. Label
   `gateway`. This one rung recovers full metadata on Vercel AI Gateway, most of it on vLLM, and
   costs one extra parse. `deepseek-harness`'s `packages/llm/llm-pi-ai/src/discovery.ts` does
   exactly this and is worth copying wholesale.
3. **Native side-channel by endpoint type.** If `/api/tags` answers, you are talking to Ollama —
   use `/api/show` per model. If `/v1/models` rows carry `max_model_len`, you are talking to vLLM.
   Sniff, do not configure. `hermes-agent`'s `should_use_ollama_native_catalog` in
   `hermes_cli/models.py` is a clean version of this, complete with a **negative cache** so a
   non-Ollama endpoint is not re-probed on every refresh.
4. **`/public/litellm_model_cost_map`** (no auth) and/or an external catalog. Label
   `catalog:<name>@<version>`. See Part 3.
5. **Operator hints file.** Label `operator`. Keep it — it is the only place a person can assert
   something about a private deployment — but it should be the *last* rung, not the second, and it
   should ship **empty**.

---

## Part 3 — Where the missing metadata should come from

Aleph's `model_hints.json` is a committed table of model names, context windows, capability flags
and publisher list prices. The module docstring is admirably clear about why that is dangerous, and
then ships one anyway. The escape is that this data now exists as a **fetched catalog**, which
satisfies "no committed list" in substance and not just in letter.

### models.dev

Fetched live on 19 Aug 2026 from `https://models.dev/api.json`: **192 providers, 6,838 models**,
~4 MB of JSON. Per model: `id`, `name`, `description`, `family`, `attachment` (accepts file
attachments), `reasoning`, `reasoning_options[{type: effort|toggle|budget_tokens, values/min/max}]`,
`tool_call`, `structured_output`, `temperature` (whether the parameter is honoured),
`knowledge` (training cutoff), `release_date`, `last_updated`, `modalities{input[], output[]}`,
`open_weights`, `limit{context, output}`, `cost{input, output, cache_read, cache_write}` in dollars
per million tokens.

`opencode` consumes this as its *only* model catalog — see
`~/Documents/code/inspiration/opencode/packages/core/src/models-dev.ts`. Its handling is the pattern
to copy:

- Fetch from a configurable source (`OPENCODE_MODELS_URL`, defaulting to a project-controlled
  mirror, not `models.dev` directly).
- Persist to a cache file; freshness TTL of 5 minutes for the "should I refetch" check, background
  refresh every 60 minutes.
- **Cross-process file lock** around the fetch, with a re-check under the lock, because several
  processes race on the same cache file. Aleph has an API process and a workers process. Same race.
- **Atomic write**: temp file then rename, temp file removed on failure.
- **Build-time snapshot fallback** (`OPENCODE_MODELS_DEV`) if the network is unavailable and no
  cache exists, plus a hard `OPENCODE_DISABLE_MODELS_FETCH` kill switch and an
  `OPENCODE_MODELS_PATH` local override.

That last set is what makes an air-gapped or offline docker-compose deployment work without turning
the catalog into a hard boot dependency.

### LiteLLM's cost map

`https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`, or
`GET /public/litellm_model_cost_map` on your own gateway with no auth. 3,055 entries. Richer on the
billing dimensions than models.dev (tiered rates, batch rates, priority/flex rates, per-image and
per-second rates, `prompt_cache_min_tokens`) and it carries `deprecation_date` on 335 entries and
`output_vector_size` on 60 embedders. LiteLLM itself refreshes this at runtime — `POST
/reload/model_cost_map`, `POST /schedule/model_cost_map_reload?hours=N`,
`LITELLM_MODEL_COST_MAP_URL`, `LITELLM_LOCAL_MODEL_COST_MAP=True` — which is a direct endorsement of
"catalog fetched at runtime" as the correct shape.

Reading it from your own gateway's public route is strictly better than reading it from GitHub: it
is the same map your gateway is using to compute the spend it reports back to you, so your ledger
and its ledger agree.

### The join problem, stated honestly

A catalog is keyed by canonical model names. Your gateway serves *aliases*. On LiteLLM the alias is
whatever the operator typed in `config.yaml` — often the canonical name, sometimes `fast`,
sometimes `bedrock-claude-sonnet-4-6`. So the join is heuristic and must be *labelled as heuristic*:

1. **Exact id match** against the catalog. High confidence.
2. **Upstream id.** `/model/info` gives `litellm_params.model`, e.g. `bedrock/anthropic.claude-…`;
   vLLM gives `root` (the HF repo id); OpenRouter gives `hugging_face_id` and `canonical_slug`.
   These are real join keys, and using them is far better than string-munging the alias. Aleph
   already extracts the provider prefix from `litellm_params.model` but **throws away the rest of
   the string** — the part that would actually match a catalog.
3. **Normalised-name match** (strip provider prefixes, region prefixes like `us.`/`eu.`, dated
   suffixes like `-20260814`). Medium confidence.
4. **No match.** Say so. Do not guess.

Confidence must be recorded and shown. A catalog-derived price on a fuzzy name match is a *worse*
fact than "unpriced", because it looks authoritative.

### What the catalog must never do

Never let a catalog override a gateway-reported value. Aleph's hints module already has this rule
(`apply_hints` only fills unset fields) and it is exactly right — keep it, and extend it to catalogs.
The precedence is: **probe result (demotion only) > gateway-reported > catalog > operator hint >
unknown.**

---

## Part 4 — Capability probing

Metadata is absent (ids-only), stale (model changed), or wrong (operator typo, gateway advertises a
model it cannot reach). Probing is calling the thing to find out. Aleph already probes for
reachability. That is one rung of five.

### Design principles

**A probe can demote, never promote — except from unknown.** If the gateway says
`supports_function_calling: true` and one probe fails, that is one sample against a stated fact.
Mark it *contested* and surface it; do not silently flip the flag, because the failure might have
been a 429. If the gateway said nothing (`unknown`) and the probe succeeds, promote to `true` with
provenance `probed`. This asymmetry is what keeps a transient network blip from permanently
blacklisting your best model.

**Probes are ledger events.** Each one is a billed model call. Aleph's own rule ("every LLM call
writes a `ModelCall` + `CostLedgerEvent`") applies to probes. Today `probe_model` calls the gateway
directly with `httpx` and writes nothing, so probing is invisible spend. That is the exact hole the
pricing module was written to close, reopened in a different file.

### The probe ladder

**L0 — reachability.** One minimal completion. Distinguishes "advertised" from "actually works".
Aleph has this. What it needs:

- Do **not** send `max_tokens: 4` unconditionally. Of OpenRouter's 415 models, **11 do not accept
  `max_tokens` at all** (including `openai/gpt-5.2-codex`, `openai/gpt-5.1-codex-max`,
  `openai/gpt-5.2-chat`) and would 400 — Aleph would record them as unreachable. Another **88 have
  `reasoning.mandatory: true`**, meaning they always emit reasoning tokens, so a 4-token budget is
  below their floor. **87 do not accept `temperature`.** Send the minimal legal body, retry once
  without the offending parameter on a 400 that names it, and classify a parameter rejection as
  "reachable, restricted parameters" rather than "unreachable".
- Classify the failure. `403 access denied` and "requires an inference profile" are **durable** —
  cache them for weeks. `429`, `503`, `timeout` are **transient** — cache for an hour, retry.
  Collapsing both into one `unreachable: dict[str, str]` loses the distinction that decides whether
  to ever try again.

**L1 — tool calling.** One request with a single trivial tool and a *forced* `tool_choice`:

```
tools: [{type: "function", function: {name: "ack",
         parameters: {type: "object", properties: {ok: {type: "boolean"}}, required: ["ok"]}}}]
tool_choice: {type: "function", function: {name: "ack"}}
```

Three outcomes, all meaningful: a `tool_calls` array back → supported; a 400 mentioning `tools` or
`tool_choice` → unsupported; **plain text back** → the third state, "accepts the parameter and
ignores it", which is the failure mode that breaks agents at runtime and which no metadata field
anywhere reports. Cost: roughly 60–80 input tokens and a handful of output tokens.

**L2 — structured output.** `response_format: {type: "json_schema", json_schema: {..., strict:
true}}` with a two-field schema. On 400, retry with `{type: "json_object"}`. Yields three levels —
strict schema / loose JSON / none — which is exactly the distinction that matters for extraction
work and which the single boolean `supports_response_schema` flattens. External testing in May 2026
put structured-output pass rates at 75–91% across 244 models, with most failures at providers with
no `json_schema` support at the API level; a boolean is not enough.

**L3 — vision.** A 1×1 PNG as a `data:` URI plus "reply with ok". A 400/415 means no vision. Note
that most providers bill a fixed minimum image-token charge (commonly ~85 tokens) regardless of
image size, so this is the most expensive probe and should only run for models a vision capability
would actually consider.

**L4 — context window. Do not probe this by sending a long prompt.** Binary-searching a context
window costs real money and real minutes. Use the **error-message oracle** instead: send a tiny
prompt with an absurd `max_tokens` (e.g. 100,000,000). Most OpenAI-compatible servers — vLLM,
LiteLLM, OpenAI — reject it *before inference* with a message naming the limit ("This model's
maximum context length is 128000 tokens…"). Parse the number. Cost: zero, because nothing was
generated. Some servers clamp silently instead of erroring; in that case fall through to the
catalog. Record which happened.

**L5 — embedding dimension.** `POST /v1/embeddings` with `input: ["a"]`, read
`len(data[0].embedding)`. Exact, costs one token, no ambiguity. **This should be the only source of
truth for embedding width.** Aleph currently keeps a `KNOWN_EMBEDDING_DIMS` dict of eight hardcoded
model names in `packages/aleph-rks/src/aleph_rks/embedding.py` in front of exactly this probe. That
is a committed model list, in a codebase whose stated rule is that there are none.

### Cost, concretely

At Opus-tier rates (~$5/Mtok in, $25/Mtok out as of the current catalog) the full L0–L2 ladder is
roughly 150 input and 20 output tokens: **about $0.0013 per model**. Forty models is five cents.
Even at OpenRouter's 415 models it is well under a dollar. **Probing is cheap. Unbounded concurrency
is what is expensive.**

`apps/api/src/aleph_api/routes/model_profile.py:143` fires `asyncio.gather` over *every* discovered
model with no semaphore. Against a 415-model gateway that is 415 simultaneous chat completions
through one `httpx` client: connection-pool exhaustion, a rate-limit storm, possibly an abuse flag,
and 45-second timeouts held open. Bound it at 4–8 concurrent, add jitter, and stop early on repeated
429s.

### Caching and invalidation

**Cache key** = `hash(gateway_base_url) + hash(credential_id) + model_id + probe_suite_version`.
The credential belongs in the key because a different virtual key sees a different model set with
different access. The suite version belongs in the key because changing what a probe *asks*
invalidates every result it produced.

**Storage**: Postgres, not process memory. Probe results cost money and must survive a restart and be
shared between the API process and the workers process. Aleph's `GatewayCatalog` holds a 300-second
in-memory TTL per process; the two processes will therefore disagree, and every restart re-pays.

**TTLs, split by outcome**:

| Outcome | TTL | Why |
|---|---|---|
| Capability confirmed | 30 days | Model behaviour is stable within a version. |
| Capability refuted (durable error: 400 on `tools`, 403 access denied) | 30 days | Configuration, not weather. |
| Failed transiently (429/503/timeout) | 1 hour | Weather. |
| Unknown (never probed) | n/a | Show as unknown; never treat as false. |

**Invalidate eagerly on**: the listing's `created`/`modified_at` changing for that id; the model
disappearing and reappearing; the gateway reporting a different upstream id for the same alias; a
probe-suite version bump; N consecutive runtime failures on a bound model; and an operator pressing
"re-test". Ollama's `modified_at` and OpenRouter's `created` both exist for exactly this.

---

## Part 5 — Capability-based routing

Aleph's shape is right and ahead of most of what is published: `CapabilityPolicy` expresses
**requirements over metadata** (mode, min context, needs-vision, needs-tools) rather than model
names, hard-filters candidates, then ranks the survivors. Keep the shape. Four changes.

### 1. Three-valued capability logic

This is the single most important correction. `DiscoveredModel` declares
`supports_vision: bool = False`, and `parse_v1_models` (`discovery.py:229`) constructs every
ids-only model with all four flags `False`. Downstream, `candidates_for` filters with
`not policy.needs_vision or m.supports_vision`. So a model nobody has described is treated as a
model that has been described as incapable, and a gateway that only serves `/v1/models` produces
"no model on this gateway qualified for any capability" — which is a false statement dressed as a
careful one.

Make every capability field `bool | None`. Then a policy can say what it wants for unknowns:
`REQUIRE` (only proven), `ALLOW_UNKNOWN` (bind it, flag it, probe it), `PROBE_FIRST` (resolve the
unknown before deciding). Aleph has already proven it knows this pattern — `metadata_available` and
`rates_source` exist for precisely this reason on the pricing side. Extend it one field over.

### 2. Replace the sort chain with a scored decision that explains itself

`candidates_for` (`discovery.py:396–441`) applies six `sort` calls in sequence, relying on stability,
including `ok.sort(key=lambda m: not m.is_priced)` three separate times. It is correct today and
nobody will be able to modify it safely. Worse, one step is actively wrong:

```python
ok.sort(key=lambda m: m.id, reverse=policy.tier == "heavy")
```

with the comment "heavy prefers the LAST id, which on every version scheme in practice means the
newer model". String ordering breaks the moment a minor version reaches double digits:
`"claude-opus-4-10" < "claude-opus-4-9"` lexically, so the *older* model wins. Both models.dev and
OpenRouter publish `release_date` / `created`; use the date, and where there is no date, do not
pretend an alphabetical tiebreak means recency — call it what it is, a stable arbitrary tiebreak.

Replace the chain with one scoring function of named weighted terms, and **persist the reason**:

```
chosen: claude-opus-4-7
because: mode=chat ✓ · context 1,000,000 ≥ 100,000 ✓ · tools ✓ (gateway) ·
         reasoning ✓ (gateway) · $5.00/$25.00 per Mtok (gateway)
rejected: bedrock-claude-sonnet-4-6 — probe failed: requires inference profile
          tiny-chat-8k — context 8,192 < 100,000
```

Every mature router in the current literature converges on this shape: hard capability gates plus a
transparent score. "LLM Routing as Reasoning: A MaxSAT View" (arXiv 2603.13612, Mar 2026) formalises
it as weighted MaxSAT over **partially observable** model attributes — Aleph's exact situation —
with hard constraints from requirements and soft constraints from preferences. "Trust by Design:
Skill Profiles for Transparent, Cost-Aware LLM Routing" (arXiv 2602.02386, Feb 2026) argues the same
case for explainability. You do not need a SAT solver for nine capabilities and forty models; you do
need the discipline of separating hard from soft and recording why.

### 3. Fail loudly. Never downgrade silently.

Aleph already leaves a capability unbound rather than binding a model that cannot do the job, and
raises at resolution time. **That is the right answer and this document is not going to argue for
the other one.** The case for it is short: a silent downgrade converts a configuration error into a
quality regression, and quality regressions in a research system are invisible until someone reads
the output carefully, which is weeks later and after the citations are wrong. An unbound capability
fails in one second at the first call, names itself, and is fixed in one minute.

Two improvements to *how* it fails. First, the error should be actionable — not
`"model profile has no binding for capability 'vision'"` but `"capability 'vision' is unbound: no
model on gateway <url> reports vision support; 12 of 18 models have unknown capabilities — run
capability probes, or set supports_vision in your hints file"`. Second, **runtime fallback is a
different question from configuration fallback and Aleph currently has neither.** `ResolvedBinding`
carries a `fallback` field, `binding_for()` writes one for every capability during autoconfigure,
and **nothing anywhere reads `.fallback`** — I grepped `packages/` and `apps/` and there are zero
readers outside the writer. That is a producer with no consumer, the defect class `CLAUDE.md` names
as this codebase's dominant one, sitting in the middle of the model path. Either wire it into
`LiteLLMClient.chat()` (retry the fallback on a 5xx/429/model-unavailable, never on a 4xx that
indicates a bad request, and record which model actually served the call on the `ModelCall` row) or
delete the field.

### 4. Read the deprecation date

335 entries in LiteLLM's cost map carry `deprecation_date`; OpenRouter publishes `expiration_date`.
Aleph reads neither, so it will confidently bind a model that stops existing next month, and find
out during a research run. A binding whose model has a deprecation date inside 90 days should be
shown in amber in Settings and mentioned in the autoconfigure result.

---

## Part 6 — Standards: there is no standard

Reported honestly, because the answer is useful: **as of August 2026 there is no standard for
serving model capability metadata over an API, and no live proposal to create one.**

- **OpenAI's `/v1/models`** is the de-facto standard shape and it deliberately carries nothing but
  ids. Community requests to add capability fields remain unimplemented.
- **LiteLLM** declined to enrich `/v1/models` (issue #21855, closed as not planned) and declined the
  framing that model discovery belongs to the LLM API (issue #20581, closed as not planned) — while
  nonetheless shipping `/model/info` into `llm_api_routes` in v1.96.0. There is no spec, only a
  behaviour.
- **OpenRouter** has the richest schema and a genuine capability query
  (`?supported_parameters=tools`). It is a vendor schema. Nobody else implements it.
- **Vercel AI Gateway** independently invented a *different* set of extension field names in the
  same envelope (`context_window` vs OpenRouter's `context_length`; `type` vs `mode`;
  `pricing.input` vs `pricing.prompt`). Two rich implementations, zero agreement.
- **models.dev** is the closest thing to a shared vocabulary, and it is a community JSON file, not a
  specification. It has real gravitational pull — `opencode` depends on it exclusively,
  `prime-agent` generates from it, `hermes-agent` reads it — which makes its field names the nearest
  thing to a lingua franca. That is worth something, and it is not a standard.
- **MCP** has active discovery SEPs (SEP-1649, server cards at
  `/.well-known/mcp/server-card.json`; SEP-1960, a manifest at `/.well-known/mcp`) dated Feb 2026,
  but those describe *servers and tools*, not model capabilities. Do not expect them to solve this.
- **Hugging Face model cards** carry structured YAML front-matter, but they describe an artefact in
  a repo, not a model behind an endpoint, and self-hosted deployments are exactly where the endpoint
  and the card diverge.

**Practical conclusion**: write an adapter layer with a normalised internal shape, populate it from
whichever field names a given endpoint happens to use, and record which one it was. Do not wait for
a standard and do not design as though one is coming.

---

## Part 7 — Configuration UX

What the operator should see, in priority order.

**One screen, capabilities down the left.** Aleph's current Settings drawer already does this and it
is the right layout. Each row: capability name, one-line description of the job, the bound model, and
the price.

**A provenance chip on every fact, not just price.** Today price is labelled
(`gateway`/`static`/`unknown`) and capability is not. Every number and flag should carry a small
marker: `gateway` (it told us), `catalog` (models.dev said so), `probed` (we tested it), `operator`
(you asserted it), `unknown`. This is the single highest-value UI change, because it converts "the
system says 200k context" into "the system is guessing 200k context", which is a completely different
sentence for someone deciding whether to trust a run.

**Show the rejected candidates and why.** The current UI shows only eligible models per capability
and a bare "unsupported by gateway" when the list is empty. An operator hitting that has no idea
whether the gateway is broken, the key is scoped wrong, or the models genuinely lack a feature. Show:
"18 models on this gateway; 6 are eligible; 12 rejected — 9 for unknown capabilities (never probed),
2 for context window, 1 unreachable." With a "probe the unknowns" button next to it.

**Preview before apply.** `autoconfigure` currently replaces the entire `bindings_jsonb` in one shot
and, if the embedding binding changed, silently enqueues `reembed_job` — a full re-embedding of the
corpus, which is the most expensive single operation the system can perform. It should render a
diff (`synthesis: claude-opus-4-6 → claude-opus-4-7`), state the consequence in words ("this changes
the embedding model, which re-embeds 47,000 chunks at an estimated $X"), and require confirmation.

**Progressive disclosure.** Default view: three or four presets ("cheapest that works", "best
available", "local only"). Advanced view: per-capability dropdowns. Expert view: the raw
requirements policy. Most operators should never see a per-token rate.

**Make discovery legible as an event.** A "last discovered: 4 minutes ago · 18 models · from
/model/info" line with a refresh button, and an explicit error state that names which rung of the
ladder was reached. Aleph's empty-state copy already does the hardest part of this well ("Aleph
ships no built-in model list, so there is nothing to choose from until it responds") — extend it to
say *which endpoint answered and which did not*.

---

## Part 8 — Verdict on `packages/aleph-models/`

### Where it is right, and ahead

- **Requirements over names.** `CAPABILITY_POLICIES` expresses each capability as constraints on
  discovered metadata. This is the correct architecture and most shipped systems do not have it.
- **Provenance on price.** `rates_source` (`gateway` / `hints` / `none`), `pricing_source` on every
  `ModelCall`, and `CostBreakdown.priced` so an unpriced call is never a silent `$0`. This is better
  than any reference implementation I read.
- **Refusal as a design principle.** Unbound beats wrongly-bound; unpriced models rank last but are
  not excluded outright (with the reasoning recorded in the comment); `unpriced_bindings()` and
  `unbound_capabilities()` surface what was refused. `select_default_bindings` is the rare function
  whose docstring explains what it declines to do.
- **Probing before binding at all.** `probe_model` exists because a gateway's list states
  configuration, not reachability. Almost nothing else does this.
- **Hints never override the gateway.** `apply_hints` fills only unset fields. Exactly right.
- **Deterministic ordering.** Same gateway, same defaults. This matters more than it looks.
- **Tolerant parsing.** One malformed row does not take discovery down.
- **The docstrings are the real asset.** They record *observed failures on real gateways* — the
  Bedrock name mismatch, the 3× rate error, the inference-profile requirement, the silent $0. That
  institutional memory is worth more than the code and must survive any rewrite.

### Where it is behind

1. **The 403 premise is stale.** The entire module is organised around `/model/info` being
   admin-only. LiteLLM v1.96.0 (2026-08-10) put it in `llm_api_routes`. Aleph still logs
   `gateway.model_info_forbidden` and degrades. It should also *detect and report the gateway
   version*, so the operator learns "upgrade your gateway and you get exact rates" rather than
   living on hints forever.
2. **`/v1/models` extensions are discarded.** `parse_v1_models` (`discovery.py:196–241`) reads only
   `id`. On Vercel AI Gateway that throws away context window, pricing, modalities and supported
   parameters that were sitting in the same JSON. On vLLM it throws away `max_model_len` — the one
   authoritative context number in the entire stack — and `root`, the catalog join key.
3. **Only two endpoint shapes are understood.** No Ollama `/api/tags` + `/api/show`, no vLLM
   awareness, no `/public/litellm_model_cost_map` (which needs no auth at all), no external catalog.
   For a system whose constraint is "connect to whatever endpoint is there", it understands one.
4. **The hints file is a committed model list.** It has all the properties the module's own
   docstrings condemn: publisher list prices, model names chosen when the code was written, a
   maintenance burden nobody will carry. models.dev and LiteLLM's cost map make it unnecessary.
   Ship the file empty.
5. **`KNOWN_EMBEDDING_DIMS`** in `packages/aleph-rks/src/aleph_rks/embedding.py` is a second
   committed model list, in front of a probe that answers the question exactly.

### Where it is wrong

1. **Unknown capability is stored as `False`.** `discovery.py:229` and the `bool` typing of the four
   `supports_*` fields. This is the same defect as "unknown price is $0", one field over, and it is
   the reason an ids-only gateway configures to nothing. Fix: `bool | None` end to end.
2. **`ResolvedBinding.fallback` has no reader.** Written by `binding_for()`, stored on every
   autoconfigured binding, read by nothing in `packages/` or `apps/`. Wire it or delete it.
3. **Unbounded probe concurrency.** `routes/model_profile.py:143`, `asyncio.gather` over every model
   with `probe=True` by default.
4. **The probe body is not portable.** `max_tokens: 4` unconditionally (`discovery.py:332`). 11 of
   OpenRouter's 415 models reject `max_tokens` outright; 88 have mandatory reasoning and cannot
   produce anything within 4 tokens. Those become false "unreachable" verdicts against the models
   Aleph most wants for heavy capabilities.
5. **Probes are unledgered spend.** `probe_model` posts to `/v1/chat/completions` with raw `httpx`
   and writes no `ModelCall` / `CostLedgerEvent`, contradicting the project's own rule.
6. **Reachability failures are not classified.** `unreachable: dict[str, str]` merges a permanent
   403 with a transient timeout, so neither can be cached correctly.
7. **Alphabetical id as a recency proxy.** `discovery.py:425`. Breaks at version 10.
8. **The `/v1` duplication trap.** `discover_models` and `LiteLLMClient` both assume `base_url`
   carries no `/v1` and append it. Every provider's documentation shows a base URL *with* `/v1`
   (`https://api.openai.com/v1`). An operator who pastes the documented form gets
   `…/v1/v1/models` → 404 → "the gateway advertises no models", with no hint about the real cause.
   The agent path has a test pinning this (`test_agent_gateway_base_url.py`); discovery does not.
   Normalise once, tolerate both, and probe both forms on first contact.
9. **Two parsers for one endpoint.** `discovery.parse_v1_models` and
   `LiteLLMClient.list_models` (`client.py:239`) independently read `/v1/models`. They will drift.
10. **The cache is per-process and in-memory.** `GatewayCatalog` holds a 300s TTL in one process;
    API and workers will disagree, and every restart re-queries. Discovery results — and especially
    probe results, which cost money — belong in Postgres.
11. **Deprecation dates are ignored.** Available from two catalogs, read from neither.
12. **`blended_cost`'s 3:1 input weighting is a hardcoded assumption** about traffic shape, derived
    from a workload (wiki pages into a selector) that is being deleted. Compute it from observed
    `ModelCall` rows instead — Aleph already has the data.

### What the reference implementations do differently

- **`opencode`** does not interrogate endpoints at all: it consumes models.dev exclusively, with a
  disciplined cache (file lock, atomic rename, TTL, background refresh, offline snapshot, kill
  switch). Catalog-first. Its weakness is a gateway serving models the catalog has never heard of.
- **`deepseek-harness`** (`packages/llm/llm-pi-ai/src/discovery.ts`) is the closest match to Aleph's
  situation and the best single file to read: catalog-first for known providers, wire interrogation
  only for unknown gateways; reads `/v1/models` *extension* fields (`context_window`,
  `context_length`, `max_tokens`, `max_output_tokens`); a hard 4 MiB response ceiling enforced on
  bytes actually read rather than the declared `Content-Length`, because the URL is user-supplied;
  an explicit protocol allowlist that refuses to guess for Azure and OAuth providers and reports
  "cannot be interrogated" rather than "no models"; and — importantly — discovery produces
  *candidate* metadata offered for adoption, never a silent config write.
- **`hermes-agent`** (`hermes_cli/models.py`, `providers.py`, `model_selection_guards.py`) has the
  best operational furniture: per-endpoint-type sniffing with a **negative cache** so a non-Ollama
  URL is not re-probed every refresh; a single **selection-guard registry** so every surface (CLI,
  TUI, web, chat) evaluates the same warnings instead of each wiring them by hand; and
  selection-time confirmation for expensive models and for data-training tiers. Aleph has one
  configuration surface today and will have more; the registry pattern is the cheap insurance.
- **`prime-agent`** generates a `models.generated.ts` at build time from models.dev + Vercel AI
  Gateway + OpenRouter. Do not copy this — it is the committed table Aleph's constraint forbids,
  merely produced by a script.

---

## What Aleph should do

1. **Make every capability field three-valued (`true` / `false` / `unknown`) end to end**, and let
   each `CapabilityPolicy` state its unknown-handling (`REQUIRE`, `ALLOW_UNKNOWN`, `PROBE_FIRST`).
   This is the highest-value change in this document. It is the same correction Aleph already made
   for price, applied to capability.
2. **Extend the degradation ladder to five rungs**: `/model/info` → `/v1/models` *with extension
   fields* → native side-channel (Ollama `/api/show`, vLLM `max_model_len` + `root`) →
   `/public/litellm_model_cost_map` and an external catalog → operator hints. Record which rung
   answered, per model, and show it.
3. **Parse `/v1/models` extension fields.** One function change recovers full metadata on Vercel AI
   Gateway and the authoritative context window on vLLM. Copy the field-candidate approach from
   `deepseek-harness/packages/llm/llm-pi-ai/src/discovery.ts`.
4. **Replace `model_hints.json` with a fetched catalog.** models.dev (`https://models.dev/api.json`)
   as primary, LiteLLM's cost map (via your own gateway's unauthenticated
   `/public/litellm_model_cost_map`) as the billing-detail source. Keep the hints *mechanism* for
   operator assertions about private deployments; ship the file empty. Copy opencode's cache
   discipline — file lock, atomic write, TTL, background refresh, offline snapshot, kill switch —
   because API and workers will race on it.
5. **Join to catalogs on upstream ids, not aliases**, and record match confidence. `litellm_params.model`,
   vLLM's `root`, OpenRouter's `hugging_face_id` / `canonical_slug` are real keys. Never let a
   fuzzy-matched catalog price be labelled as anything but fuzzy.
6. **Build the probe ladder (L0–L5)** described in Part 4, with the demote-only rule, outcome-split
   TTLs, and results in Postgres keyed by gateway + credential + model + suite version.
7. **Bound probe concurrency at 4–8** with jitter and early stop on repeated 429s, and route every
   probe through `LiteLLMClient` so it writes a `ModelCall` + `CostLedgerEvent` like every other
   call.
8. **Make the probe body portable**: minimal legal request, retry once without a parameter the
   server rejected by name, and classify parameter rejections separately from unreachability.
9. **Replace the six-`sort` chain with one scored decision that records its reasoning**, persist the
   `because` / `rejected` explanation on the binding, and render it in Settings. Use `release_date`
   for recency, never alphabetical id order.
10. **Wire `ResolvedBinding.fallback` into `LiteLLMClient.chat()`** (retry on 5xx/429/unavailable,
    never on a 4xx bad request, record the model that actually served the call) — or delete the
    field.
11. **Delete `KNOWN_EMBEDDING_DIMS`** and use the one-token embedding probe as the sole source of
    vector width, cached with the other probe results.
12. **Read deprecation dates** from the catalog and warn in Settings when a bound model expires
    within 90 days.
13. **Normalise the base URL once**, tolerating both `…:4000` and `…:4000/v1`, and add a test
    mirroring `test_agent_gateway_base_url.py` for the discovery path. Collapse
    `LiteLLMClient.list_models` into the discovery parser so there is one reader of `/v1/models`.
14. **Add a provenance chip to every fact in the Settings UI**, show rejected candidates with
    reasons and a "probe the unknowns" button, and put a diff-and-confirm step in front of
    `autoconfigure` that names the re-embedding cost before triggering it.
15. **Detect and report the gateway's LiteLLM version.** "You are on v1.94; upgrading to ≥1.96 lets
    Aleph read exact per-token rates" is a far better operator experience than permanently degraded
    discovery with no explanation.

## What Aleph should avoid

1. **Do not treat unknown as false.** Not for capability, not for price, not for context window. It
   is the mistake this module was written to fix and it is still present in the capability fields.
2. **Do not commit a model list or a price list — including as a "hints" file with real numbers in
   it.** A fetched catalog with recorded provenance satisfies the constraint; a committed table does
   not, however it is labelled.
3. **Do not let a catalog or a hint override a gateway-reported value.** Precedence is: probe
   (demotion only) > gateway > catalog > operator hint > unknown. The existing `apply_hints`
   ordering is correct; keep it as catalogs are added.
4. **Do not make the external catalog a hard boot dependency.** Cached, snapshotted, kill-switched,
   and non-fatal when unreachable — as pricing discovery already is.
5. **Do not probe context windows by sending long prompts.** Use the `max_tokens`-overflow error
   oracle, or the catalog. Binary search over a context window is the most expensive way to learn a
   number that is usually published.
6. **Do not fan out probes without a concurrency bound.** 415 simultaneous billed completions is a
   rate-limit storm and an abuse flag, not a configuration step.
7. **Do not let a single probe failure permanently demote a gateway-reported capability.** Mark it
   contested, cache transient failures for an hour, and let the operator see the conflict.
8. **Do not silently downgrade to a lesser model.** Aleph's fail-loud stance is correct — keep it,
   and make the error name what is missing and what would fix it. Runtime fallback on a 5xx is a
   different thing and is fine, provided the ledger records which model actually answered.
9. **Do not add another parser for an endpoint that already has one.** Two readers of `/v1/models`
   will drift, and the drift will surface as a binding that validates and fails at runtime.
10. **Do not assume LiteLLM.** Ollama and vLLM are first-class targets under the stated constraints
    and neither speaks LiteLLM's admin surface. Discovery must degrade to something useful against a
    bare OpenAI-compatible server with no admin routes at all.
11. **Do not trust `/api/show`'s `context_length` as the served window.** It is the trained window;
    Ollama's runtime default is smaller and VRAM-dependent. Prefer the error-message oracle or an
    explicit `num_ctx`.
12. **Do not spend probe money without recording it.** A probe is a model call. Same ledger, same
    rules.

---

## Sources

Live fetches, 19 Aug 2026: `https://openrouter.ai/api/v1/models` (415 models),
`https://openrouter.ai/api/v1/models/{id}/endpoints`, `https://openrouter.ai/api/v1/providers`,
`https://models.dev/api.json` (192 providers / 6,838 models),
`https://ai-gateway.vercel.sh/v1/models` (348 models),
`https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`
(3,055 entries).

Source read directly: `BerriAI/litellm` `litellm/proxy/_types.py` at tags v1.91.0, v1.92.0, v1.93.0,
v1.94.0, v1.95.1, v1.96.0, v1.96.2, v1.97.0 and `main`; `litellm/utils.py`;
`litellm/proxy/management_endpoints/model_management_endpoints.py`; `vllm-project/vllm`
`vllm/entrypoints/openai/engine/protocol.py` (`ModelCard`), `.../openai/models/serving.py`,
`.../serve/tokenize/api_router.py`.

Docs: LiteLLM model discovery, model management, AI Hub, custom model cost map, auto-sync;
Ollama `/api/show` reference and context-length page; vLLM OpenAI-compatible server; AWS Bedrock
`ListFoundationModels` / `ListInferenceProfiles`.

Issues: BerriAI/litellm #21855 (closed, not planned), #20581 (closed, not planned via PR #20766).

Papers: arXiv 2603.13612 (MaxSAT routing over partially observable attributes, Mar 2026);
arXiv 2602.02386 (BELLA, skill profiles for transparent cost-aware routing, Feb 2026);
arXiv 2608.06867 (LLMRouter, Aug 2026).

Local reference implementations: `~/Documents/code/inspiration/opencode/packages/core/src/models-dev.ts`;
`~/Documents/code/inspiration/deepseek-harness/packages/llm/llm-pi-ai/src/discovery.ts`;
`~/Documents/code/inspiration/hermes-agent/hermes_cli/{models,providers,model_selection_guards,model_cost_guard}.py`;
`~/Documents/code/inspiration/prime-agent/packages/ai/scripts/generate-models.ts`.

Aleph code reviewed: `packages/aleph-models/src/aleph_models/{discovery,hints,pricing,profile,client}.py`,
`packages/aleph-models/src/aleph_models/model_hints.json`,
`packages/aleph-models/tests/test_discovery.py`,
`apps/api/src/aleph_api/routes/model_profile.py`,
`packages/aleph-core/src/aleph_core/schemas/model_profile.py`,
`packages/aleph-runtime/src/aleph_runtime/capabilities.py`,
`packages/aleph-rks/src/aleph_rks/embedding.py`,
`apps/web/src/components/Drawers.tsx`.
