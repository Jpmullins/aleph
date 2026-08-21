# Aleph's model layer — a concrete design

Written 19 August 2026, against the findings in `docs/research/openai-compatible-endpoints.md` and
`docs/research/model-discovery.md`, and against the tree as merged at `bcc478a`.

This is a design document, not a survey. Where it states a version number or an endpoint behaviour,
that fact came from one of the two research files above and was verified live on 19 Aug 2026 there.
Where it makes a design claim, the claim is mine and the reasoning is shown.

---

## In one paragraph

Aleph serves no models, so its model layer is entirely a layer of *knowledge and discipline about
somebody else's server*. The design has five moving parts and one rule. The parts: an **Endpoint**
(one URL, one credential, one set of established facts); a **Fact Store** that records, per model,
what is known, how it came to be known, and — crucially — what is still *unknown*; a **Prober** that
converts unknowns into facts by spending a fraction of a cent; a **Binder** that turns a job
("summarise", "embed") into a decision about which model serves it, and writes down why; and a
**Transport** that is the only code in Aleph allowed to open a socket to a model, and which is
therefore also the credential boundary and the accounting boundary. The rule is that **nothing is
ever assumed to be false because it is unknown, and nothing is ever assumed to be free because it is
unpriced** — a distinction Aleph already gets right for price and gets wrong, one field over, for
capability. Everything above the Transport is a plugin: where facts come from, how models are
probed, how candidates are scored, which wire protocol a binding speaks. Everything at and below the
Transport is core and protected: the socket, the credential, the ledger write. That split is what
makes the layer swappable without making it slow, because plugins act once per *endpoint* or once
per *request*, and never once per token.

---

## The words, defined once

- **Endpoint** — an HTTP address Aleph talks to, plus the credential it presents. Always
  OpenAI-compatible in shape. Might be a *gateway* that fans out to many providers (LiteLLM,
  OpenRouter) or a *server* running models on one box (Ollama, vLLM).
- **Model** — an id the endpoint will accept in the `model` field of a request. That is all it is.
  Everything else about it is a claim someone made.
- **Capability** — a *job*, in Aleph's vocabulary: `synthesis`, `judge`, `extraction`, `embedding`.
  Call sites ask for a job. They never name a model. This is already Aleph's design and it is right.
- **Fact** — one thing believed about one model on one endpoint (its context window, whether it can
  call tools, what it costs), together with *where that belief came from* and *when*.
- **Provenance** — where a fact came from: reported by the gateway, read from a public catalog,
  asserted by the operator, established by an actual test, or unknown.
- **Probe** — actually calling a model to find out something the metadata did not say, or said
  wrongly. A probe costs money. It is a real model call and must be recorded as one.
- **Binding** — the recorded decision that capability X is served by model Y on endpoint Z over
  protocol P, with the reason it was chosen and the list of what was rejected.
- **Embedding space** — a set of vectors all produced by the same embedding model at the same width.
  Vectors from two different spaces cannot be compared, and comparing them raises no error.

---

## 1. The shape of the model layer

### 1.1 The picture

Two paths run through this layer and they have completely different performance characteristics.
Confusing them is the main way people build a slow plugin system.

**The warm path** — what happens on every LLM call. Everything here is resolved data and a socket.

```
  call sites: agents, RKS ingest, reviewers, research loop, evals
  ────────────────────────────────────────────────────────────────
        they ask for a JOB, never for a model:
        gateway.chat(capability=SYNTHESIS, project=…, purpose="research.compose")
                              │
                              ▼
        ┌──────────────────────────────────────────────┐
        │  ModelGateway            [CORE, protected]   │  the only surface call sites see
        │  chat · stream · embed · rerank · count      │
        └───────┬──────────────────────────┬───────────┘
                │ 1. resolve               │ 2. open an accounting scope
                ▼                          ▼
        ┌───────────────┐          ┌───────────────────────────────┐
        │  Resolver     │          │  Accountant   [CORE, protected]│
        │  [CORE]       │          │  a ModelCall row is written    │
        │  capability   │          │  on EVERY exit path, including │
        │  → Binding    │          │  exception, abort and timeout  │
        │  (dict lookup)│          └───────────────┬───────────────┘
        └───────┬───────┘                          │
                └──────────────┬───────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │  Transport               [CORE, protected]   │
        │  one pooled httpx client · three timeout     │
        │  clocks · classified retry · SSE reader      │
        │  · the credential never leaves this object   │
        └───────┬──────────────────────────┬───────────┘
                │ shapes the body          │ classifies the error
                ▼                          ▼
        ┌──────────────────┐      ┌──────────────────────┐
        │ ProtocolAdapter  │      │ ErrorClassifier      │
        │   [PLUGIN]       │      │   [PLUGIN]           │
        │ chat.completions │      └──────────────────────┘
        │ responses        │
        │ cohere.rerank    │
        └──────────────────┘
                │
                ▼
        the operator's endpoint (LiteLLM / vLLM / Ollama / OpenRouter / …)
```

**The cold path** — what happens when an operator points Aleph somewhere, or presses "re-discover",
or a nightly refresh runs. Nothing here is on a request's critical path.

```
  Endpoint{url, credential, id}
        │
        ├─► EndpointDoctor      [CORE]    eight-step reachability diagnosis (§2.3)
        │
        ├─► EndpointSniffer     [PLUGIN]  → kind: litellm | vllm | ollama | openrouter | plain
        │
        ├─► MetadataSource chain  [PLUGINS, ordered by declared rank]
        │       rank 10  gateway /model/info                 provenance = gateway
        │       rank 20  /v1/models incl. extension fields   provenance = gateway
        │       rank 30  native side channel (Ollama /api/show, vLLM max_model_len)
        │       rank 40  public catalog (models.dev, LiteLLM cost map)  provenance = catalog
        │       rank 50  operator hints file                 provenance = operator
        │                     │
        │                     ▼  each yields Fact(value, provenance, observed_at, confidence)
        ├─► FactStore          [CORE, Postgres]
        │       one row per (endpoint_id, model_id, field)
        │       three-valued: true / false / UNKNOWN — never bool-defaulted
        │                     │
        ├─► Prober             [CORE runner] executing ProbeStep plugins L0…L5
        │       results are Facts with provenance = probed
        │       every probe goes through ModelGateway, so every probe is ledgered
        │                     │
        └─► Binder             [CORE contract] + SelectionStrategy [PLUGIN]
                │
                ▼
            BindingProposal{ per capability: chosen, reason, rejected[…] }
                │  ← a PROPOSAL. Never a silent write.
                ▼
            operator confirms  →  ModelProfile row + ActionLedgerEvent
```

### 1.2 What each component owns

| Component | Owns | Must not know |
|---|---|---|
| **Endpoint** | one base URL (normalised once), one credential handle, an identity used as a cache key, a locality classification (local vs remote) | anything about models |
| **EndpointDoctor** | the ordered reachability diagnosis and its human-readable messages | how models are chosen |
| **EndpointSniffer** | recognising a server kind from its responses (never from configuration) | anything it cannot observe |
| **MetadataSource** | producing `Fact`s for models on an endpoint | how facts are stored, ranked, or used |
| **FactStore** | the durable record of what is known, by whom, when, at what confidence; the three-valued semantics; TTL by outcome class | where facts came from |
| **Prober / ProbeStep** | turning one unknown into one fact by making one real call | binding policy |
| **Requirements (`CapabilityPolicy`)** | what a job *needs*, stated over facts, never over model names | which models exist |
| **Binder + SelectionStrategy** | ranking survivors, recording the reason and the rejections | how to make an HTTP call |
| **ModelProfile** | the persisted per-project capability → binding map | how bindings were derived |
| **Resolver** | capability → `Binding`, in microseconds, from memory | the network |
| **Transport** | the socket, the pool, the three timeout clocks, retry mechanics, SSE reading, the credential | what a capability means |
| **ProtocolAdapter** | shaping a request body and parsing a response for one wire protocol | the credential, the ledger |
| **Accountant** | the guarantee that a call cannot finish without a row; usage extraction and estimation; `usage_source` | pricing rates |
| **PricingResolver** | rates and their provenance; the arithmetic | tokens |
| **EmbeddingSpaceRegistry** | which embedding space is active, at what width, produced by which model, and whether a vector may be written | retrieval ranking |

The two ownership rules that carry the most weight:

1. **Only the Transport holds the credential.** Everything above it receives a *bound client*, never
   a key. In a kernel with scoped capability access this is the difference between a plugin that can
   make a model call and a plugin that can exfiltrate a key.
2. **Only the Accountant writes a `ModelCall`,** and the Transport has no code path that reaches the
   network outside an open accounting scope. That is structural, not a convention — see §6.

### 1.3 What changes versus today

Today one global `LITELLM_BASE_URL` + one key serve every capability, and `ModelBindingIn.provider`
is regex-pinned to the literal string `"litellm"`. The design above makes the **endpoint part of the
binding**. That is not gold-plating; it is what the stated constraint implies. Three concrete things
it buys, all of which people actually want:

- A local Ollama for `classification` and `extraction`, a cloud gateway for `synthesis`.
- Reranking, which has **no OpenAI endpoint at all** — the de facto standard is Cohere's
  `/v1/rerank`, served by vLLM and LiteLLM, and by neither Ollama nor llama.cpp. It cannot be assumed
  to live on the chat endpoint, so it needs its own.
- An embedding endpoint that stays pinned while the chat endpoint is swapped, which is the single
  most dangerous configuration change in the system (§7).

---

## 2. Cold start: from an empty `.env` to a working configuration

### 2.1 What the operator actually types

Exactly two values, and one of them is optional:

```
ALEPH_MODEL_ENDPOINT_URL=http://host.docker.internal:11434/v1
ALEPH_MODEL_ENDPOINT_KEY_FILE=/run/secrets/model_endpoint_key   # preferred
ALEPH_MODEL_ENDPOINT_KEY=sk-...                                 # dev fallback
```

Notes on each, because each has bitten someone:

- **Rename from `LITELLM_BASE_URL` / `INSIGHTS_LITELLM_API_KEY`.** A vendor-named variable
  contradicts the constraint that the endpoint is interchangeable, and the current name is actively
  misleading to an operator pointing at bare vLLM. Keep the old names readable for one release,
  logged as deprecated.
- **Accept the URL with or without a trailing `/v1`.** Every vendor's documentation shows the base
  URL *with* it. Today both `discover_models` and `LiteLLMClient` append `/v1` unconditionally, so
  an operator who pastes the documented form gets `…/v1/v1/models` → 404 → "the gateway advertises no
  models", with no hint about the real cause. Normalise once, in one function, and on first contact
  try both forms and remember which answered.
- **`_FILE` beats env.** The `_FILE` suffix convention is what Postgres, Redis and most official
  images use, so it needs no explanation. It removes the key from the process environment and from
  `docker inspect`, which is the bulk of the accidental-leak surface. Read the file **at use time**,
  not at import time, so a rotated key needs no restart. Hold it in a wrapper type whose `__repr__`
  is `***` so it cannot land in a structlog field or a traceback by accident.
- **One anchored `extra_hosts: ["host.docker.internal:host-gateway"]`** on every service that talks
  to models. It is redundant on Docker Desktop, required on Linux, and irrelevant for a remote
  endpoint — so one line covers all four deployment shapes and the operator only ever edits the URL.

### 2.2 Boot, and what is allowed to refuse to boot

This is a genuine tension with the kernel rule that *a capability which cannot answer a live query
must not come up*, and it needs resolving explicitly rather than by accident.

Split today's single `models` capability into three:

```
models.transport   [protected, core]
    probe: the URL parses, the credential is readable, an httpx pool exists.
    Does NOT touch the network. Always comes up if configuration is well-formed.

models.discovery   [core]
    requires models.transport.
    probe: the endpoint answered a listing call within the last N minutes,
           or a cached FactStore snapshot exists for this endpoint identity.
    Comes up on a cached snapshot. Fails when the endpoint has never answered.

models.bound       [core]
    requires models.discovery + db.
    probe: for the default profile, every bound capability resolves AND its
           model answered a one-token call within its probe TTL.
    Fails when nothing is bound, or when a bound model has gone away.
```

Why the split matters: if the API process refuses to boot on an unreachable endpoint, the only tool
that can fix the endpoint — the Settings UI — is unreachable too. So `models.transport` boots
unconditionally and the HTTP surface plus Settings depend only on it. Everything that would produce
garbage without a working model — ingest workers, the research loop, reviewers, the assistant — declares
`requires={"models.bound"}` and therefore **does not come up**, truthfully, with the kernel's own
support-set machinery reporting why:

```
aleph-api  up · 19 capabilities active · 6 inactive
   models.bound  FAILED — endpoint http://host.docker.internal:11434/v1 answered
     GET /v1/models with 0 models. 6 capabilities are down as collateral:
     rks.ingest, research.loop, reviewer.verify, assistant.chat,
     belief.extract, evals.runner
```

That is a much better failure than a process that boots green and produces nothing.

### 2.3 First contact: the endpoint doctor

Runs from **inside a container**, because a check that runs on the host tests a different network
path than Aleph uses. Walk in order, report the *first* step that fails, with a cause and a fix —
never a stack trace.

| Step | What it checks | Example message |
|---|---|---|
| 1 | URL parses, has a scheme | `"localhost:11434" is missing a scheme. Use http://host.docker.internal:11434/v1` |
| 2 | DNS resolves from inside the container | `Cannot resolve "host.docker.internal" from inside the stack. On Linux this needs extra_hosts: ["host.docker.internal:host-gateway"].` |
| 3 | TCP connects | `Resolved → 172.17.0.1, nothing listening on :11434.` Then three ranked causes with exact commands: server not running; bound to 127.0.0.1 (`OLLAMA_HOST=0.0.0.0`); host firewall dropping the bridge (`ufw allow from 172.17.0.0/16`). |
| 4 | It speaks HTTP and something OpenAI-shaped | `Connected, but GET /v1/models returned 502. Is that the right port?` |
| 5 | Auth | `401. A key is set (12 chars, starts "sk-abc…") and was rejected.` **Never print the key.** |
| 6 | The list is non-empty | `Reachable and authenticated, but zero models. Your gateway has no models configured, or your key is scoped to none.` |
| 7 | Capability coverage | `4 models. No model could be bound for "embedding": none reports an embedding mode and none answered an embedding probe. Retrieval will not work. Models seen: a, b, c, d.` |
| 8 | Per-model probe | `"bedrock-claude-sonnet-4-6" is advertised but returned "requires an inference profile" when invoked. Skipping it.` |

Steps 6–8 already exist in Aleph as `scripts/verify-gateway.sh` and `aleph_models.discovery`. That
script's header comment is the best writing in the repo on this subject — it learned the right
lesson, that an advertised model is not a reachable one. Keep the logic; move the transport inside
the stack; surface the result in the UI, not only in a terminal the operator may never open.

### 2.4 The full cold-start sequence

```
 0. operator sets ALEPH_MODEL_ENDPOINT_URL (+ key) and runs `docker compose up -d --wait`
 1. models.transport comes up. API + Settings are reachable. Nothing else models-related is.
 2. EndpointDoctor runs steps 1–6. Failures render in Settings with the fix text.
 3. EndpointSniffer classifies the endpoint from its responses.
 4. MetadataSource chain runs in rank order. Every field lands in the FactStore
    with a provenance and a timestamp. Fields nobody supplied stay UNKNOWN.
 5. Binder computes a candidate set per capability from facts alone.
    → capabilities with a clear winner on gateway-reported facts need no probe.
    → capabilities whose candidates have UNKNOWN in a required field are queued
      for probing.
 6. Probe plan is shown BEFORE it runs:
      "12 models have unknown tool support. Probing costs ~$0.016 and ~40 s.
       [Probe]  [Skip — these will stay unbindable for synthesis/extraction]"
    Runs at concurrency 4–8 with jitter, stops early on repeated 429s, and is
    hard-capped by a budget (default $0.25 / 200 calls per run).
 7. Binder produces a PROPOSAL — a diff, with reasons and rejections:
      synthesis     (unbound) → qwen3-72b-instruct
        because  mode=chat ✓ · ctx 131,072 ≥ 100,000 (gateway) ·
                 tools ✓ (probed 19 Aug) · $0.40/$1.20 per Mtok (catalog, exact-id match)
        rejected llama3-8b        ctx 8,192 < 100,000
                 mixtral-8x7b     tools: probe returned plain text (accepted and ignored)
                 nomic-embed-text mode=embedding
      embedding     (unbound) → bge-m3
        ⚠ binding this creates embedding space #1 at width 1024 (probed).
 8. operator confirms → ModelProfile written + ActionLedgerEvent + models.bound activates,
    whose probe makes one real call per bound capability. Green means green.
 9. anything that was down as collateral now activates, in dependency order.
```

**Headless first boot.** `ALEPH_AUTOCONFIGURE=on-first-boot` runs steps 3–8 with auto-confirm, under
the same rules and the same probe budget, with one exception it must never make: it will create the
*first* embedding space, but it will never *change* an existing one. See §7.

---

## 3. Discovery, probing and binding

### 3.1 The one correction that matters most

Aleph fixed "unknown price silently means $0" and then made the identical mistake one field over.
`DiscoveredModel` declares `supports_vision: bool = False`, and `parse_v1_models`
(`discovery.py:229`) constructs every ids-only model with all four `supports_*` flags `False`.
`candidates_for` then filters with `not policy.needs_vision or m.supports_vision`. So **a model
nobody has described is treated as a model that has been described as incapable**, and an endpoint
that only serves `/v1/models` — which is the floor case, not the exception — produces
*"no model on this gateway qualified for any capability"*. That is a false statement dressed up as a
careful one.

Every capability field becomes three-valued end to end:

```python
type Tri = bool | None          # True | False | None == "nobody has said"

@dataclass(frozen=True)
class Fact[T]:
    value: T                    # may be None
    provenance: Provenance      # gateway | catalog | operator | probed | inferred
    observed_at: datetime
    confidence: Confidence      # exact | joined_on_upstream_id | joined_on_name | asserted
    detail: str = ""            # "catalog models.dev@2026-08-19, matched on root=BAAI/bge-m3"
```

And each `CapabilityPolicy` declares what it does about unknowns, per requirement:

```python
class Unknowns(StrEnum):
    REQUIRE       = "require"        # only proven-true models qualify
    PROBE_FIRST   = "probe_first"    # resolve the unknown, then decide (the default)
    ALLOW_FLAGGED = "allow_flagged"  # bind it, mark the binding provisional, probe in background
```

`PROBE_FIRST` is the right default because a probe costs about $0.0013 and removes the ambiguity
permanently. `ALLOW_FLAGGED` exists for the air-gapped case where probing is impossible or the
operator has said no.

### 3.2 The discovery ladder — five rungs, not two

Tried in order. Each records its own provenance per field. **A later rung never overrides an earlier
one for the same field**; it only fills what is still unknown. This is exactly the rule `apply_hints`
already implements, generalised.

| Rung | Source | Gets you | Provenance |
|---|---|---|---|
| 1 | `GET /model/info` | modes, context windows, capability flags, exact per-token rates including cache rates | `gateway` |
| 2 | `GET /v1/models`, **parsed for extensions** | ids everywhere; on Vercel AI Gateway also `context_window`, `pricing`, `modalities`, `supported_parameters`; on vLLM `max_model_len` (authoritative) and `root` (the catalog join key) | `gateway` |
| 3 | native side channel | Ollama `/api/show` → `capabilities[]`, embedding width; vLLM's `max_model_len` | `gateway` |
| 4 | public catalog | models.dev (192 providers / 6,838 models) and LiteLLM's cost map (3,055 entries, incl. `deprecation_date` and `output_vector_size`) | `catalog` |
| 5 | operator hints file | anything a person asserts about a private deployment | `operator` |

Three things about this ladder that are easy to get wrong.

**Rung 1's premise is stale and Aleph should detect that, not assume it.** The entire current module
is organised around `/model/info` being admin-gated and 403ing for an application's virtual key.
LiteLLM v1.96.0 (10 Aug 2026) added it to `llm_api_routes`, so on a current gateway a restricted key
*can* read it. But the tracked GitHub issues asking for exactly that were closed as *not planned* —
the code says yes, the issue tracker says no. So: try it, record whether it worked, and when it
403s, report the actionable version fact — *"your gateway is on v1.94; upgrading to ≥1.96 lets Aleph
read exact per-token rates"* — rather than degrading forever with no explanation.

**Rung 2 is where the biggest cheap win is.** `parse_v1_models` currently reads only `id` and throws
the rest of the JSON away. One function change recovers full metadata on Vercel AI Gateway and the
one authoritative context number in the entire stack on vLLM. Read a *candidate list* of field
names, because two rich implementations invented two different vocabularies and neither is a
standard: `context_window` / `context_length` / `max_model_len`, `max_tokens` / `max_output_tokens`,
`type` / `mode`, `pricing.input` / `pricing.prompt`.

**Rung 4 is a fetched catalog, not a committed table, and the difference is real.** The current
`model_hints.json` is a committed list of model names, context windows and publisher list prices —
which is precisely the artefact its own module docstring condemns. Ship that file **empty**, keep the
*mechanism*, and get the data from a dated, cached fetch. Copy opencode's cache discipline verbatim,
because Aleph has an API process and a workers process that will race on the same cache file: a
cross-process file lock with a re-check under the lock, an atomic temp-file-then-rename write, a TTL
for "should I refetch", a background refresh, a build-time snapshot for the offline case, a local
path override, and a hard kill switch.

**The join to a catalog is heuristic and must be labelled as heuristic.** Catalogs are keyed by
canonical model names; your gateway serves aliases the operator typed. Join in this order and record
which one worked: exact id → upstream id (`litellm_params.model`, vLLM's `root`, OpenRouter's
`hugging_face_id`) → normalised name (strip provider, region and date suffixes) → no match, say so.
Aleph today extracts the provider prefix from `litellm_params.model` and **throws away the rest of
the string**, which is the part that would actually match. A catalog price on a fuzzy name match is
*worse* than "unpriced", because it looks authoritative.

And one hard rule specific to rates: **capability facts are properties of the model and safe to
inherit from a catalog; rates are properties of the deployment and are only ever an estimate.** A
self-hosted vLLM serving a model whose id happens to match an OpenAI one must not inherit OpenAI's
prices. When the endpoint looks local — loopback, `host.docker.internal`, any unqualified hostname
(i.e. a Docker Compose service name), RFC-1918, link-local, Tailscale CGNAT — suppress catalog
pricing entirely and mark it `unknown`.

### 3.3 The probe ladder

Metadata is absent, stale, or wrong. Probing is calling the thing to find out. Aleph probes for
reachability today; that is one rung of six.

| | Probe | Method | Cost | Answers |
|---|---|---|---|---|
| **L0** | reachability | minimal legal completion | ~20 tok | advertised vs actually works; which max-tokens field is accepted |
| **L1** | tool calling | one trivial tool, `tool_choice` forced to it | ~80 tok | supported / rejected / **accepted-and-ignored** |
| **L2** | structured output | `json_schema` + `strict`, fall back to `json_object` on 400 | ~60 tok | strict schema / loose JSON / none |
| **L3** | vision | 1×1 PNG data URI | ~100 tok (image minimum) | vision yes/no. Only for vision candidates — this is the expensive one |
| **L4** | context window | tiny prompt with an absurd `max_tokens` (100,000,000) | **0** | most servers reject *before inference* with a message naming the limit; parse the number |
| **L5** | embedding width + norm | embed the single token `"a"` | 1 tok | exact dimension, and whether the vector is unit-norm |

Four rules that make the ladder trustworthy:

**A probe can demote, never promote — except from unknown.** If the gateway says
`supports_function_calling: true` and one probe fails, that is one sample against a stated fact:
mark it **contested**, surface the conflict, do not silently flip it — the failure might have been a
429. If the field was unknown and the probe succeeds, promote to `true` with provenance `probed`.
This asymmetry is what stops a transient network blip from permanently blacklisting the best model
on the endpoint.

**L1 exists because of the third outcome.** vLLM documents that it accepts `strict: true` on a tool
schema and *ignores it*. Ollama accepts `tool_choice` and ignores it. Getting plain text back from a
forced tool call is the state that produces a shipped-but-inert feature with every step reporting
success — the defect class `CLAUDE.md` already names as this codebase's dominant one — and **no
metadata field anywhere reports it.** Only the probe finds it.

**The probe body must be portable.** Today it sends `max_tokens: 4` unconditionally. Of OpenRouter's
415 models, 11 reject `max_tokens` outright and 88 have mandatory reasoning and cannot produce
anything inside a 4-token budget — so exactly the heavy models Aleph most wants get falsely recorded
as unreachable. Send the minimal legal body; on a 400 that *names* an offending parameter, retry once
without it and record "reachable, restricted parameters" rather than "unreachable".

**Never binary-search a context window by sending long prompts.** It costs real money and real
minutes to learn a number that is usually published. Use the L4 error oracle, or the catalog. When a
server clamps silently instead of erroring, record *that* and fall through.

### 3.4 Storing and invalidating probe results

Probe results cost money, so they belong in Postgres — shared between the API and workers processes,
surviving a restart. `GatewayCatalog`'s 300-second in-process TTL means the two processes disagree
and every restart re-pays.

```
cache key = endpoint_identity  ⊕  credential_id  ⊕  model_id  ⊕  probe_suite_version
```

The credential belongs in the key because a different virtual key sees a different model set with
different access. The suite version belongs in the key because changing what a probe *asks*
invalidates every result it produced.

| Outcome | TTL | Why |
|---|---|---|
| capability confirmed | 30 days | model behaviour is stable within a version |
| capability refuted, durable error (400 naming `tools`, 403 access denied, "requires inference profile") | 30 days | configuration, not weather |
| failed transiently (429, 503, timeout) | 1 hour | weather |
| never probed | — | show as **unknown**, never as false |

Invalidate eagerly when: the listing's `created`/`modified_at` changes for that id; the model
disappears and reappears; the gateway reports a different upstream id for the same alias; the probe
suite version bumps; N consecutive runtime failures on a bound model; or an operator presses
"re-test".

### 3.5 Binding: a scored decision that explains itself

Keep the shape Aleph already has — **requirements over discovered metadata, never model names** —
and replace the mechanism. `candidates_for` currently applies six `sort()` calls in sequence relying
on stability, including `ok.sort(key=lambda m: not m.is_priced)` three separate times. It is correct
today and nobody will be able to modify it safely. One step is also actively wrong:

```python
ok.sort(key=lambda m: m.id, reverse=policy.tier == "heavy")   # discovery.py:425
```

with a comment claiming the last id is the newer model. String ordering breaks the moment a minor
version reaches double digits: `"claude-opus-4-10" < "claude-opus-4-9"`, so the *older* model wins.
Both catalogs publish `release_date`; use it, and where there is no date, call an alphabetical
tiebreak what it is — a stable arbitrary tiebreak, not a recency proxy.

Replace with one scoring function of named, weighted terms, split cleanly into **hard gates** (drop
the candidate) and **soft preferences** (order the survivors), and persist the reasoning:

```
Binding{
  capability: "synthesis",
  endpoint_id: …, model: "qwen3-72b-instruct", protocol: "chat.completions",
  chosen_because: [
      "mode=chat (gateway)",
      "context 131,072 ≥ required 100,000 (gateway)",
      "tools ✓ (probed 2026-08-19)",
      "reasoning ✓ (catalog models.dev@2026-08-19, exact id match)",
      "$0.40 / $1.20 per Mtok (catalog — estimate, not this deployment's bill)",
  ],
  rejected: [
      ("llama3-8b",       "context 8,192 < 100,000"),
      ("mixtral-8x7b",    "tools: forced call returned plain text — accepted and ignored"),
      ("claude-opus-4-7", "unreachable: 403 access denied (durable, cached to 2026-09-18)"),
  ],
  provisional: false,      # true when bound on an unresolved unknown
  established_by: ["source:gateway", "source:models_dev", "probe:l1_tools"],
}
```

`established_by` matters for the plugin story: it is how deactivating a metadata source knows which
bindings to mark for re-verification (§5.4).

Two more binder duties nothing does today:

- **Read deprecation dates.** 335 entries in LiteLLM's cost map carry `deprecation_date`; OpenRouter
  publishes `expiration_date`. Aleph reads neither, so it will confidently bind a model that stops
  existing next month and find out mid-run. A binding whose model expires within 90 days shows amber
  in Settings and is named in the autoconfigure result.
- **Record the protocol.** `chat.completions` is the only protocol you can assume across every
  endpoint an operator might point you at; `/v1/responses` is where reasoning-model features land and
  where OpenAI's own agent tooling has moved (Codex dropped chat completions in Feb 2026), and Ollama
  serves it stateless-only. The same deployment will have models reachable only over one. Make the
  protocol **a property of the binding**, not a global switch.

---

## 4. Failure behaviour, stated as explicitly as success

Five principles, then the table.

1. **Unknown is never false, and unpriced is never $0.**
2. **Refuse to bind rather than bind something that cannot do the job.** A silent downgrade converts a
   configuration error into a quality regression, and quality regressions in a research system are
   invisible until someone reads the output carefully — which is weeks later, after the citations are
   wrong. An unbound capability fails in one second at the first call, names itself, and is fixed in
   one minute. Aleph already takes this position; keep it.
3. **Fail at bind time where possible, at first call otherwise, never silently mid-run.**
4. **Every failure is a ledger row.** A failed attempt costs money at least as often as it does not.
5. **Every error message names what was observed and what would fix it.** Not
   `"model profile has no binding for capability 'vision'"` but *"capability 'vision' is unbound: no
   model on <endpoint> reports vision support; 12 of 18 models have unknown capabilities — run
   capability probes, or assert supports_vision in your hints file."*

| Condition | Behaviour | Recorded | Operator sees |
|---|---|---|---|
| URL unparseable / no scheme | `models.transport` probe fails; process refuses to boot | boot log | Compose stops with the fix text |
| Credential file missing/unreadable | same | boot log | `_FILE points at /run/secrets/x which does not exist` |
| DNS / TCP / non-HTTP failure | `models.discovery` fails; `models.bound` and its dependents stay down; API + Settings up | endpoint health row | doctor step 2–4 message + blast radius list |
| 401 / 403 on the listing | same | endpoint health row | doctor step 5, key length + prefix only |
| Endpoint lists zero models | same | endpoint health row | doctor step 6 |
| `/model/info` 403 | ladder falls to rung 2; **not** an error | per-field provenance | *"exact rates unavailable — your gateway may predate v1.96"* |
| Catalog fetch fails | fall through to the next rung; never fatal | fetch attempt log | *"catalog unavailable, using cached snapshot from 2026-08-12"* |
| Catalog joins only fuzzily | fact stored at `joined_on_name` confidence; **pricing suppressed** | confidence on the fact | fuzzy chip on the fact in Settings |
| No model satisfies a capability | capability left **unbound**; other capabilities still bind | proposal `unbound[]` + ledger | *"18 models; 6 eligible; 12 rejected — 9 unknown capabilities, 2 context window, 1 unreachable"* + **[Probe the unknowns]** |
| Probe fails, durable class | model excluded; cached 30 days | probe fact + `ModelCall` | listed under rejected with the reason and expiry |
| Probe fails, transient class | model **not** excluded; cached 1 hour; retried | probe fact + `ModelCall` | *"unverified — last attempt timed out, will retry"* |
| Probe contradicts a gateway-reported flag | fact marked **contested**; not silently flipped | both facts retained | conflict shown with both sources and a re-test button |
| Probe budget exhausted | probing stops cleanly; remaining models stay unknown | probe-run summary | *"stopped after 200 calls / $0.25. 7 models still unprobed."* |
| Capability unbound at call time | `CapabilityUnbound` raised immediately, message per principle 5 | no `ModelCall` (nothing was called) | the actionable error, surfaced in the run |
| Bound model 404s (`model_not_found`) | **no retry** — retrying is pure waste. Fall over to the binding's fallback if present, else fail. Mark the binding `stale` and trigger re-discovery | `ModelCall` per attempt with `served_model` | *"synthesis was bound to X; the endpoint no longer serves it. Re-configure."* |
| 429 | honour `Retry-After` (capped at ~120 s), full jitter, bounded attempts; then fall over to a different **model**, not a different credential — an aggregator's throttle follows the credential | `ModelCall` per attempt | rate-limit state in the run view |
| 5xx before any tokens | retry same model | `ModelCall` per attempt | — |
| 5xx **after** tokens were emitted | **never retry.** Surface the partial and let the caller decide; a retry re-bills the whole prompt and yields a second partial answer | `ModelCall`, `outcome=partial`, `usage_source=reported_partial` | partial output marked as truncated |
| Stream stalls | per-chunk idle timeout fires (30–120 s), abort the controller, cancel the reader | `ModelCall`, `usage_source=reported_partial`, output tokens counted from deltas received | *"the model stopped sending for 90 s"* |
| Context overflow | **compress, do not fail over** — a different model with the same window will fail identically | `ModelCall` + compression event | — |
| Content-policy block | deterministic; do not retry | `ModelCall`, classified | reason shown verbatim |
| TLS/cert failure | deterministic per host; fail fast rather than burning three identical handshakes | classified error | *"the endpoint's certificate did not verify"* + the two real fixes |
| Fallback chain exhausted | 5-second cooldown before accepting a resubmit, so a client loop cannot re-marshal an 80k-token context once per provider per turn and drive the host into swap | classified error | — |
| Usage absent from the response | row still written; `usage_source=estimated`, input counted client-side, output counted from deltas | `ModelCall` | *estimated* chip on the cost |
| Model unpriced | row still written; `cost_usd` **null**, `pricing_source=unknown`, loud log | `ModelCall` | *unknown* chip; never `$0.00` |
| Embedding width ≠ active space width | **the write is refused before the call is billed**; the source stays in the stale set as a durable, queryable mark | `rks.reembed.dim_mismatch_skipped` + counter | *"embedder produces 1536-d vectors; space #1 is 1024-d. Create a new space to switch."* |
| Bound embedder disappears | retrieval **reads** keep working (existing vectors are fine); ingest of new documents stops, loudly | endpoint health + ingest failure | *"ingest paused: the embedder for the active space is unreachable"* |
| Metadata-source plugin deactivated | facts it established are marked `orphaned`; bindings that depended solely on them go `needs_reverify` but keep serving | fact provenance + blast radius | list of affected bindings with a re-verify button |
| Model deprecates within 90 days | binding still serves; amber warning | binding annotation | *"expires 2026-11-01 — pick a replacement"* |

Two deliberate non-behaviours, stated so nobody adds them later thinking they were forgotten:

- **No automatic silent re-binding.** When discovery notices a better model, it produces a proposal.
  A research system whose model changed under it without a recorded decision cannot explain its own
  outputs.
- **No automatic embedding cutover, ever.** See §7.

---

## 5. Making this a plugin layer rather than hardwiring

### 5.1 The dividing line

The split is not "big things are core, small things are plugins". It is: **anything that can lose you
money or corrupt a persistent index is core and protected; anything that is a strategy for talking to
somebody else's server is a plugin.**

**CORE — mounted from the boot manifest with `protected=True`, cannot be deactivated or replaced:**

| Core piece | Why it cannot be a plugin |
|---|---|
| `ModelGateway` interface | it is the contract every call site codes against; swapping it is not a plugin change, it is a rewrite |
| `Transport` (socket, pool, credential) | it is the credential boundary. A plugin that owns the socket owns the key |
| `Accountant` + `CallScope` | it is the only structural guarantee that spend cannot go unrecorded. If it is removable, it will be removed "temporarily" |
| `FactStore` + `Fact` / `Provenance` types | the three-valued semantics are the invariant everything else rests on |
| `Binding` / `Requirements` types | the vocabulary plugins speak |
| `EmbeddingSpaceRegistry` | it is the only thing standing between an operator and a silently corrupted index |

**PLUGINS — ordinary kernel capabilities, each with its own probe, activatable and revertible:**

| Plugin kind | Interface | Examples | Rank/selection |
|---|---|---|---|
| `EndpointSniffer` | `sniff(endpoint) -> EndpointKind \| None` | litellm, vllm, ollama, openrouter | first non-None wins; negative results cached so a non-Ollama URL is not re-probed each refresh |
| `MetadataSource` | `facts(endpoint, models) -> Iterable[Fact]` | `gateway_model_info`, `v1_models_extended`, `ollama_native`, `models_dev`, `litellm_cost_map`, `operator_hints` | ordered by declared `rank`; earlier ranks win per field |
| `ProbeStep` | `id, cost_estimate, applies_to(facts), run(client, model) -> Fact` | L0…L5 | the runner executes applicable steps in id order |
| `ProtocolAdapter` | `build_request(...)`, `parse_response(...)`, `parse_stream(...)` | `chat.completions`, `responses`, `anthropic.messages`, `cohere.rerank` | selected by `Binding.protocol` |
| `SelectionStrategy` | `score(policy, candidates) -> ranked + reasons` | `default_scored`, `cheapest_that_works`, `local_only`, `best_available` | one active at a time; changing it produces a proposal, never a silent re-bind |
| `PricingSource` | `rates(endpoint, model) -> Fact[Rates]` | gateway-reported, catalog, operator | ordered like metadata sources |
| `ErrorClassifier` | `classify(exc \| response) -> Failure` | default taxonomy; per-vendor extensions | chain until classified |
| `TokenCounter` | `count(messages, model) -> int` | tiktoken-ish approximation, vLLM `/tokenize`, llama.cpp `/tokenize` | best available for the endpoint |

### 5.2 How a plugin is registered

Each is a `CapabilitySpec` — the mechanism already exists and already enforces the important things
(a probe is mandatory, `protected` cannot be self-declared, dependants are computed, teardown unwinds
LIFO and runs every inverse even when one raises).

```python
def models_dev_catalog(rank: int = 40) -> CapabilitySpec:
    async def setup(ctx):
        source = ModelsDevSource(cache_dir=..., ttl_s=3600, kill_switch=...)
        registry: SourceRegistry = ctx.get(MODEL_SOURCE_REGISTRY)
        token = registry.add(source, rank=rank)
        yield lambda: registry.remove(token)          # the inverse, authored next to the effect
        ctx.provide("models.source.models_dev", source)

    async def probe(ctx) -> ProbeResult:
        # exercise the READ path: the cached catalog must actually answer a lookup
        src = ctx.get("models.source.models_dev")
        hit = await src.lookup("gpt-4o-mini")
        if hit is None:
            return problem("catalog loaded but returned nothing for a known id")
        return ok(f"{src.model_count} models, snapshot {src.fetched_at:%Y-%m-%d}")

    return CapabilitySpec(name="models.source.models_dev", setup=setup, probe=probe,
                          requires=frozenset({SETTINGS, MODEL_SOURCE_REGISTRY}),
                          provides=frozenset({"models.source.models_dev"}))
```

The registry is a plain ordered list behind a lock, held by core. Plugins never mutate each other.

### 5.3 What a "different provider strategy" actually means, concretely

The test of this design is whether someone can drop in a genuinely different strategy without
touching core. Three worked examples:

- **"I only ever want models.dev, I do not want Aleph interrogating my endpoint."** Deactivate
  `models.source.gateway_model_info` and `models.source.v1_models_extended`, keep the catalog source.
  Discovery still works; every fact is labelled `catalog`; the binder marks bindings provisional
  until probed. This is roughly opencode's architecture, reachable by configuration.
- **"My shop routes through a home-grown router with its own JSON."** Ship one `MetadataSource`
  plugin plus one `ProtocolAdapter` plugin. Zero core changes.
- **"I want the agent to author a routing strategy for itself."** That is a `SelectionStrategy`
  plugin. It reads facts and returns a ranking with reasons; it cannot open a socket, cannot see the
  credential, and cannot write a binding — it produces a proposal. This is the runtime-plugin thesis
  applied to the model layer with the blast radius already bounded by the interface.

### 5.4 Revert semantics — the part that is usually forgotten

Deactivating a plugin must not break a running system, and must not leave stale beliefs.

- **Deactivating a `MetadataSource`:** its facts are not deleted. They are marked `orphaned` (the
  source that vouched for them is gone). Bindings whose `established_by` list is now entirely
  orphaned move to `needs_reverify` and **keep serving**. Demote-only: a fact loses its voucher, not
  its value. The blast radius reports exactly which bindings are affected.
- **Replacing a `SelectionStrategy`:** bindings are not recomputed. The new strategy produces a diff
  against the current profile, which a human confirms. The kernel's `replace` already restores the
  previous plugin when the replacement's probe fails; that composes correctly here.
- **Deactivating a `ProtocolAdapter`:** refused if any live binding names that protocol. This is the
  one place the model layer needs its own guard, because the kernel's dependency graph is keyed on
  capability names and cannot see a protocol string inside a database row. Implement it as the
  adapter's own inverse checking for live bindings and raising.
- **`Accountant` and `Transport` are `protected`.** The kernel already refuses to deactivate a
  protected capability and refuses any deactivation whose collateral includes one. That is the
  enforcement.

### 5.5 On the performance worry

The owner's concern is that a plugin architecture will be slow. For this subsystem, it will not be,
and here is the specific reason rather than reassurance.

- **The network dominates by five orders of magnitude.** A chat call is 0.3–120 seconds of remote
  compute. Resolving a capability to a binding is a dict lookup on a frozen dataclass held in memory
  — microseconds. The existing `LiteLLMClient` already does an OTEL span, a Redis idempotency check
  and two database writes per call, every one of which costs more than the entire plugin dispatch.
- **Plugins run on the cold path or once per request, never per token.** Metadata sources and probes
  run at discovery time. The protocol adapter is selected once from the already-resolved binding and
  builds one request body. **SSE deltas pass straight through the Transport to the caller with no
  plugin in the loop.** Write that down as a rule, because it is the one boundary that would actually
  cost you: routing every token through a dynamically-loaded transform is how a plugin system becomes
  slow.
- **Where the latency actually hides**, in descending order: (1) creating a new HTTP client — and
  therefore a new TLS handshake — per call instead of reusing the pool, worth 50–200 ms per request
  and a genuinely common bug; (2) a probe on the hot path instead of at bind time; (3) parsing a 4 MB
  catalog synchronously on the event loop (parse it in a thread, once, and keep the lookup index);
  (4) unbatched embedding calls.
- **It is demonstrated, not theoretical.** opencode dynamic-imports provider modules by npm package
  name and can install unknown providers from npm at runtime, inside a latency-sensitive interactive
  TUI. prime-agent ships `registerApiProvider` / `unregisterApiProviders(sourceId)` — a
  runtime-mutable protocol table — in production.

---

## 6. Accounting that cannot silently under-report

### 6.1 The failure being designed against

Aleph's known cost hole is that `AgentCostCallbackHandler` writes a `ModelCall` only when the
response carries token usage *and* a project id resolves. The instinct to fix it by "remembering to
set `stream_options.include_usage`" does not work, for four independent reasons:

- Usage is **withheld by default** on streams; you must ask.
- Some endpoints **reject the ask** — Google's native Gemini REST endpoint 400s on `stream_options`.
- Some proxies **accept the ask and never send the chunk**.
- An **aborted stream never delivers the usage chunk at all**, and you were still billed for
  everything generated up to the abort.

And the gateway is not a safe fallback: LiteLLM currently writes **no spend row at all** for
streaming `/v1/responses` calls (issue #32487), and `x-litellm-response-cost` is a pre-flush estimate
on streams because headers must flush before the body (#12689).

### 6.2 The structural guarantee

Make it *impossible* to reach the network without an open accounting scope, rather than making it a
rule people follow.

```python
class Transport:
    async def _send(self, scope: CallScope, request: Request) -> Response:
        """The ONLY method that touches a socket. It takes a CallScope by
        signature, so a caller cannot construct a request without one."""
```

`CallScope` is only obtainable from `Accountant.call(...)`, an async context manager whose `finally`
writes the row on **every** exit path — success, exception, cancellation, timeout, abort:

```python
async with accountant.call(
    project_id=…, agent_run_id=…, capability=Capability.SYNTHESIS,
    binding=binding, purpose="research.compose",
) as scope:
    scope.record_input_estimate(counter.count(messages))   # before dispatch
    async for delta in transport.stream(scope, req):
        scope.count_output_delta(delta)                    # as they arrive, not at the end
        yield delta
    scope.observe_usage(final_usage_chunk)                 # may never happen. That is fine.
# ← the ModelCall row is written HERE, unconditionally.
```

Three consequences worth being explicit about: an aborted stream still yields a row with a real
output-token count; a probe is a call and gets a row like everything else, closing the current hole
where `probe_model` posts raw `httpx` and records nothing; and every attempt in a retry or fallback
chain gets its own row, so the chain is visible and budgeted instead of invisible.

### 6.3 The two provenance fields

Aleph already has `pricing_source`. Add `usage_source` next to it. Together they make every dollar
figure traceable to a method.

| `usage_source` | Meaning |
|---|---|
| `reported` | the server sent `usage` |
| `reported_partial` | the stream ended early; input known, output counted from the deltas received |
| `estimated` | no usage at all; both sides counted client-side, labelled |
| `unknown` | could not even estimate — e.g. an abort before any bytes. **Still a row.** |

| `pricing_source` | Meaning |
|---|---|
| `gateway` | the deployment reported its own rates |
| `catalog` | a fetched catalog, with catalog name, version and match confidence |
| `operator` | asserted in the hints file (today's `static`) |
| `unknown` | nothing could price it |

The hard rule: **`cost_usd` is nullable and `pricing_source=unknown` means NULL, not `0`.** A zero is
a claim that the call was free. Today `CostBreakdown` returns `Decimal("0")` with `priced=False`,
which is honest at the type level but stores a number that will be summed by any dashboard that
forgets to filter. Make the column nullable so the sum is *impossible* to get silently wrong; expose
`unpriced_call_count` beside every total.

Also carry on every row: `served_model` (which model actually answered, after fallback),
`attempt_no`, `outcome` (`ok` / `partial` / `error` / `aborted`), `upstream_request_id`
(`x-request-id`, for spend disputes), and `probe_suite_id` when the call was a probe.

### 6.4 Reconciliation, not delegation

If the endpoint is LiteLLM, it keeps `LiteLLM_SpendLogs`. Treat that as a **second book to reconcile
against, never the book of record.** A nightly job compares Aleph's ledger with the gateway's per day
per model and reports drift. Drift is the signal that a whole class of calls is going unrecorded on
one side — which is exactly the bug LiteLLM shipped. Never overwrite Aleph's rows with the gateway's;
record both and the difference.

### 6.5 Prompt caching, carefully

Cache reads and cache writes are billed differently and reported inconsistently. Aleph's
`_CACHE_WRITE_KEYS` multi-spelling acceptance and its `cache_write_multiplier` are both good and
should survive. Two cautions to encode:

- `billable_input = prompt_tokens - cached_tokens` is **not universally safe**; some aggregators
  double-count `cached_tokens` against `prompt_tokens`. Clamp at zero and flag the anomaly rather
  than producing a negative.
- Self-hosted vLLM does prefix caching but **reports no `cached_tokens` on its OpenAI surface**. You
  save latency and GPU and your accounting cannot see it. Record "caching not observable on this
  endpoint" as a fact rather than as a zero.

---

## 7. The embedding-model swap, and how to make it safe

### 7.1 Why this is the most dangerous thing in the layer

A wrong chat response is one bad answer. A wrong embedding **silently corrupts a persistent index
that everything else reads**, and the damage shows up as degraded retrieval — which looks like "the
model got dumber", not "the operator changed a setting". Aleph's entire belief layer sits downstream
of retrieval, and `docs/acceptance.md` §B pins recall@1 at 0.91 as the bar. A half-migrated index
fails that bar with no error anywhere: cosine similarity across two embedding spaces does not throw,
it returns confidently ranked nonsense.

### 7.2 Where Aleph is today

| Location | Problem |
|---|---|
| `packages/aleph-rks/src/aleph_rks/models.py:34` — `EMBEDDING_DIM = 1024` | the vector width is a module constant |
| same file `:155` — `mapped_column(Vector(EMBEDDING_DIM))` | the width is **compiled into the schema**, so swapping the embedder is a migration, not a configuration change, and there is no supported path from 1024 to 1536 |
| `packages/aleph-rks/src/aleph_rks/embedding.py` — `KNOWN_EMBEDDING_DIMS` | a hardcoded nine-entry model list, in a codebase whose stated rule is that there are none — and stale by construction: `Qwen3-Embedding-8B` (4096), `nomic-embed-text` (768) and `gte-modernbert-base` (768) all fall through to "unknown" and are discovered by paying for a failed batch |
| `RetrievalIndexRecord.embedder_model` | good — but there is no `embedding_dim`, no `normalized` flag, and no notion of an *active* version that queries filter on |
| `retrieval.py` reembed guard | genuinely good shape: reads `len(chunks[0].embedding)`, rejects before paying, and deliberately leaves `embedder_model` stale so the row stays in the queryable "needs re-embed but dim-blocked" set. Keep this instinct; give it a real data model |
| `routes/model_profile.py` autoconfigure | replaces `bindings_jsonb` wholesale and, if the embedding binding changed, **silently enqueues `reembed_job`** — the most expensive single operation the system can perform, triggered as a side effect of a settings save |

### 7.3 The design: embedding spaces

Make the embedding *space* a first-class, versioned object, and make every vector name its space.

```
embedding_space
  id              uuid
  project_id      uuid
  endpoint_id     uuid          -- an embedder can live on a different endpoint
  model           text          -- "bge-m3"
  dim             int           -- PROBED, never looked up in a table
  normalized      bool          -- probed: is the returned vector unit-norm
  status          enum(building | shadow | active | retired)
  created_at, activated_at, retired_at
  eval_recall_at_1  numeric      -- filled by the gate in §7.5
  eval_run_id       uuid
```

**Physical storage.** pgvector needs a fixed dimension on the column to build an HNSW or IVFFlat
index, and Postgres partitions must share a column type — so one table cannot hold two widths and
stay indexable. The honest answer is **one physical vector table per space**, created at space
creation with the probed width and `CREATE INDEX CONCURRENTLY`, named after the space id, and read
through a repository that resolves space → table. The DDL is Aleph-emitted with a controlled
identifier; no agent-emitted SQL is involved. The alternative — a fixed set of allowed widths — is a
hardcoded list wearing a different hat.

```
                 ┌──────────────────────────────────────────┐
 ingest ────────►│ EmbeddingSpaceRegistry                   │
                 │  active(project) -> Space                │
                 │  writable(space) -> bool                 │
                 └──────┬───────────────────────┬───────────┘
                        │                       │
             during a migration, ingest writes BOTH:
                        ▼                       ▼
              space #1 (active, 1024)   space #2 (shadow, 768)
              chunk_vec_<id1>           chunk_vec_<id2>
                        ▲                       ▲
   retrieval reads ─────┘   (never both; queries filter by space_id)
```

**Invariants, enforced in code, not by convention:**

1. A vector write names a space. There is no default.
2. A vector whose length ≠ `space.dim` is refused **before** the embed call is billed. The current
   guard already has this instinct; keep it and drive it from `space.dim`, not from a dict.
3. A read filters by exactly one space. Never a union. This is the invariant that makes mixing
   impossible rather than merely discouraged.
4. Normalise client-side before storing, always, and record that you did. Some servers return
   unit-norm vectors and some do not; a half-normalised index returns numbers that are simply wrong,
   with no error.

### 7.4 Discovering the width

Delete `KNOWN_EMBEDDING_DIMS`. The width is a **probe result**: `POST /v1/embeddings` with
`input: ["a"]`, read `len(data[0].embedding)`. Exact, one token, no ambiguity, works for models
nobody has heard of. A catalog (LiteLLM's cost map carries `output_vector_size` on 60 embedders) may
*pre-warn* in the UI — "this is probably 1024-d" — but the probe decides and the probe result is what
is stored on the space.

Watch for the `dimensions` parameter (Matryoshka truncation): OpenAI's `text-embedding-3-*` return a
shortened, already-normalised vector when you pass it, Ollama supports it, vLLM is model-dependent,
llama.cpp and TGI do not. **A server that ignores `dimensions` returns the full-width vector** —
another accepted-and-ignored divergence. If Aleph ever sends `dimensions`, the probe must send it too
and measure what comes back, and the space records the *measured* width, not the requested one.

### 7.5 The swap, as a supervised migration

An embedding-model change is never a configuration edit. It is:

```
 1. operator picks a new embedder in Settings.
    Aleph probes it: dimension, normalisation, batch ceiling, reachability.
 2. a NEW space is created in `building` at the probed width.
    Nothing about the active space changes. Retrieval is untouched.
 3. backfill runs as a bounded, resumable, ledgered job with a live cost counter
    and a visible estimate up front:
      "re-embedding 47,000 chunks with bge-m3 → estimated $2.14, ~25 min. [Start]"
    New ingest during this window writes to BOTH spaces (the active one and the
    building one), so the new space is not stale on the day it goes live.
 4. space moves to `shadow`. The retrieval eval runs against BOTH:
      uv run python -m aleph_evals.retrieval_eval --space <old>  → recall@1 0.91
      uv run python -m aleph_evals.retrieval_eval --space <new>  → recall@1 0.93
    Aleph is unusually well placed here: the 45-pair labelled set and the runner
    already exist. Make "new ≥ old" a REQUIRED gate, not a report.
 5. operator confirms the cutover. `active` flips atomically. The old space goes
    to `retired` and is KEPT.
 6. rollback = flip `active` back. One statement. The old table never moved.
 7. the old space is dropped only on an explicit, separate, later action.
```

Steps 4 and 5 are what convert the single scariest operator action into a measured one.

Two more places to enforce it:

- **The `models.bound` probe embeds one token at boot** and compares the width against the active
  space. `CLAUDE.md` already says a capability that cannot answer a live query must not come up; this
  is that rule applied where it matters most.
- **Autoconfigure must never change an embedding binding as a side effect.** Today it does, and it
  fires the re-embed job from inside a settings save with `except Exception: pass` around the enqueue.
  The new rule: autoconfigure proposes an embedding change; the migration flow above is the only way
  to enact one; and headless first-boot may *create* space #1 but may never *replace* one.

---

## 8. `packages/aleph-models` — keep, change, delete

### 8.1 Keep (and carry forward verbatim into any rewrite)

- **The docstrings.** `discovery.py`, `pricing.py` and `hints.py` record *observed failures on real
  gateways*: the Bedrock name mismatch where not one model name matched, the ~3× rate error, the
  inference-profile requirement, the plausible $0.00 dashboard. That institutional memory is worth
  more than the code and must survive any restructuring. Move it, do not rewrite it.
- **`CAPABILITY_POLICIES` — requirements over names.** Each capability expressed as constraints on
  discovered metadata rather than as a model list. This is the correct architecture and most shipped
  systems do not have it.
- **Provenance on price**: `rates_source`, `pricing_source` on every `ModelCall`,
  `CostBreakdown.priced`. Better than any reference implementation surveyed. Extend, do not replace.
- **Refusal as a design principle**: unbound beats wrongly-bound; unpriced models rank last but are
  not excluded outright (the comment explaining why — a gateway whose only embedder is unpriced would
  otherwise be unusable — is exactly right); `unbound_capabilities()` and `unpriced_bindings()`
  surface what was refused.
- **`apply_hints` precedence**: fills only unset fields, never overrides the gateway. Generalise this
  rule to catalogs unchanged.
- **`get_default_pricing()` returning an EMPTY table.** There is no default price list. Keep it and
  keep the docstring explaining why a "free" local model belongs in operator hints, not in the repo.
- **`_decimal()` via `str`.** `Decimal(2.65e-06)` carries binary float error into money;
  `Decimal(str(2.65e-06))` does not.
- **Tolerant parsing** in `parse_model_info` — one malformed row must not take discovery down.
- **`ChatMessage._null_content_is_empty`.** `content: null` is normal for a tool-only turn or a
  `max_tokens` truncation; coercing to `""` while keeping truncation detectable via `finish_reason`
  is right.
- **`_CACHE_WRITE_KEYS` multi-spelling acceptance**, and the `cache_write_multiplier` reasoning that a
  cache write is billed at a premium — modelling the discount without the premium under-reports
  systematically and invisibly.
- **`GatewayCatalog.refresh_pricing` being non-fatal.** An unreachable endpoint must not stop a
  process booting; calls meanwhile record `unknown` rather than a fabricated $0.
- **The idempotency cache**, with two hardenings: record the key *before* dispatch so a crash mid-call
  cannot produce a duplicate on restart, and store the upstream `x-request-id` alongside it.

### 8.2 Change

| # | Location | Change |
|---|---|---|
| 1 | `discovery.py` `DiscoveredModel.supports_*: bool` and `parse_v1_models` (~:229) | `bool` → `bool \| None`. An ids-only model must report `None`, not `False`. **Highest-value change in this document.** |
| 2 | `discovery.py` `parse_v1_models` (~:196–241) | read extension fields, not just `id`: `context_window` / `context_length` / `max_model_len`, `max_tokens` / `max_output_tokens`, `type` / `mode`, `modalities`, `supported_parameters`, `pricing`, `tags`, `root`, `parent` |
| 3 | `discovery.py` `parse_model_info` | keep the **whole** `litellm_params.model` upstream id, not just the provider prefix — the discarded part is the catalog join key |
| 4 | `discovery.py` `candidates_for` (~:396–441) | six stability-dependent `sort()` calls → one scored decision with hard gates, soft preferences, and a persisted `because` / `rejected` explanation |
| 5 | `discovery.py:425` | delete `sort(key=lambda m: m.id, reverse=heavy)` as a recency proxy — it inverts at version 10. Use `release_date`; where absent, call it an arbitrary stable tiebreak |
| 6 | `discovery.py` `probe_model` (~:332) | portable body (no unconditional `max_tokens: 4`), retry once without a parameter the server rejected by name, classify durable vs transient failure, and **route through the accountant** so a probe writes a `ModelCall` |
| 7 | `routes/model_profile.py:143` | `asyncio.gather` over every model → semaphore of 4–8, jitter, early stop on repeated 429s, hard budget cap, and a preview of the cost before it runs |
| 8 | `discovery.py` `unreachable: dict[str, str]` | classified `Failure`, so a permanent 403 and a transient timeout can be cached differently (30 days vs 1 hour) |
| 9 | `retry.py` (all 33 lines) | classified error taxonomy: reason enum + `retryable` / `should_compress` / `should_rotate_credential` / `should_fallback`, `Retry-After` honoured with a ~120 s cap, full jitter, no retry of a stream that already emitted tokens, 5 s post-exhaustion cooldown |
| 10 | `client.py` `_post_with_retry` `timeout=120.0` | three clocks: connect 2–5 s, time-to-first-token 60–300 s, **per-chunk idle 30–120 s**, and no total deadline on a streamed generation. Auto-relax for local endpoints, treating unqualified hostnames (Compose service names) as local |
| 11 | `client.py` — no streaming API at all | add one, with the idle-timeout SSE reader and per-delta output counting. Streaming is where the accounting hole lives |
| 12 | `client.py` — no `rerank()` | either implement it against Cohere's shape with its own endpoint, or delete `Capability.RERANK`. Today the policy exists and **nothing calls it** — a producer with no consumer |
| 13 | `discovery.py` `GatewayCatalog` 300 s in-process TTL | Postgres-backed `FactStore` keyed by endpoint ⊕ credential ⊕ model ⊕ suite version, with a small per-process memo in front. API and workers currently disagree and every restart re-pays |
| 14 | `profile.py:41` `max_input_tokens` defaulting to `200_000` | that is a silent guess about someone else's model. Make it `int \| None` and force callers to handle unknown |
| 15 | `model_profile.py` `provider: str = Field(pattern=r"^litellm$")` | replace with `endpoint_id` + `protocol`. The vendor pin contradicts the constraint |
| 16 | `discovery.py` `blended_cost` 3:1 weighting | derived from wiki-page-into-selector traffic that is being deleted. Compute from observed `ModelCall` rows — Aleph has the data — or rename it so the assumption is visible |
| 17 | base-URL handling in both `discover_models` and `LiteLLMClient` | one normaliser, tolerating `/v1` present or absent, probed both ways on first contact, with a test mirroring `test_agent_gateway_base_url.py` |
| 18 | `binding_for()` writes `fallback`, nothing reads it | wire it into the call path (retry on 5xx / 429 / unavailable, **never** on a 4xx bad request, and record `served_model`) — or delete the field. Zero readers exist in `packages/` or `apps/` |
| 19 | settings `litellm_base_url` / `insights_litellm_api_key` | → `ALEPH_MODEL_ENDPOINT_URL` / `ALEPH_MODEL_ENDPOINT_KEY` + `_FILE`, read at use time, wrapped in a redacting type |
| 20 | `ChatResponse` / `EmbedResponse` | add `usage_source`, `served_model`, `upstream_request_id`; make `cost_usd` nullable rather than `"0"` |
| 21 | `pricing.py` `CostBreakdown` returning `Decimal("0")` when unpriced | return `None`. A nullable column makes a wrong sum structurally impossible rather than merely discouraged |
| 22 | `capabilities.py` single `models()` capability | split into `models.transport` / `models.discovery` / `models.bound` (§2.2) so the Settings UI stays reachable when the endpoint is wrong, while everything that needs a model honestly stays down |

### 8.3 Delete

| Location | Why |
|---|---|
| the **contents** of `aleph_models/model_hints.json` | a committed table of model names, context windows and publisher list prices — the exact artefact its own module docstring condemns. **Keep the mechanism, ship the file empty**, and get the data from a fetched, dated, cached catalog |
| `LiteLLMClient.list_models` (`client.py:239`) | a second, independent reader of `/v1/models`. Two parsers for one endpoint will drift, and the drift surfaces as a binding that validates and fails at runtime |
| `PricingTable.cost_for` (`pricing.py`) | it discards `priced` — exactly the signal that made unpriced models invisible. Its own docstring says so |
| `KNOWN_EMBEDDING_DIMS`, `is_known_embedding_model`, `embedding_dim_mismatch` (`aleph-rks/embedding.py`) | a hardcoded model list in front of a probe that answers the question exactly. Replaced by `EmbeddingSpace.dim` |
| `EMBEDDING_DIM = 1024` as a schema input (`aleph-rks/models.py:34,155`) | a compile-time constant makes an embedder swap a migration. Replaced by per-space physical tables |
| `Capability.PAGE_SELECTION` and its policy | the LLM wiki page-selector is being removed (`docs/decisions.md` D1). Its only caller is `aleph-assistant/retrieval/router.py`, which goes with it |
| `Capability.RERANK`'s policy **or** its absence of a client method | pick one. A `mode="rerank"` filter that no endpoint reports and no call site uses is dead weight either way |

### 8.4 Suggested module layout after the change

Same distribution name, same place in the DAG (`aleph-core` remains the only dependency for the pure
parts; `aleph-db` stays behind the accounting boundary).

```
aleph_models/
  endpoint.py    Endpoint, URL normalisation, locality, redacting SecretRef
  facts.py       Fact, Provenance, Confidence, Tri, FactStore protocol
  sources/       gateway.py · v1_models.py · native.py · catalog.py · hints.py
  probe.py       ProbeStep protocol, the L0–L5 suite, the bounded runner
  requirements.py CapabilityPolicy + Unknowns
  binder.py      SelectionStrategy protocol, default scorer, Binding + reasons
  transport.py   the socket, three clocks, SSE reader, credential boundary
  protocols/     chat_completions.py · responses.py · cohere_rerank.py
  errors.py      Failure taxonomy + classifier chain
  accounting.py  CallScope, Accountant, usage_source
  pricing.py     rates + provenance + arithmetic  (largely as today)
  gateway.py     ModelGateway — the one surface call sites import
```

---

## What Aleph should do

1. **Make every capability fact three-valued — `true` / `false` / `unknown` — end to end**, and let
   each capability policy state how it treats unknowns (`REQUIRE`, `PROBE_FIRST`, `ALLOW_FLAGGED`).
   This is the same correction Aleph already made for price, applied one field over, and it is the
   single highest-value change in this document: without it, a plain OpenAI-compatible server
   configures to nothing.
2. **Extend the discovery ladder to five rungs** — `/model/info` → `/v1/models` *with extension
   fields* → native side channel → fetched public catalog → operator hints — recording per-field
   provenance, and never letting a later rung override an earlier one.
3. **Ship `model_hints.json` empty and fetch the catalog instead.** models.dev and LiteLLM's public
   cost map (readable unauthenticated from your own gateway at `/public/litellm_model_cost_map`)
   satisfy the no-committed-list rule in substance. Copy opencode's cache discipline — file lock,
   atomic rename, TTL, background refresh, offline snapshot, kill switch — because API and workers
   will race on the same file.
4. **Build the L0–L5 probe ladder**, bounded at 4–8 concurrent with jitter and a budget cap, results
   in Postgres keyed by endpoint ⊕ credential ⊕ model ⊕ suite version, with outcome-split TTLs and a
   demote-only rule. Route every probe through the gateway so it writes a `ModelCall`.
5. **Replace the six-`sort` chain with a scored decision that explains itself**, persist the reason
   and the rejections on the binding, render both in Settings with a provenance chip on every fact,
   and use `release_date` for recency rather than alphabetical id order.
6. **Make the endpoint and the protocol properties of the binding**, not global settings, so rerank
   can live elsewhere, a local model can serve the cheap capabilities, and `/v1/responses` can be used
   where it exists without assuming it everywhere.
7. **Make the accounting structural**: a `CallScope` obtainable only from the `Accountant`, required
   by signature on the only method that touches a socket, writing a `ModelCall` on every exit path;
   output tokens counted from deltas as they arrive; `usage_source` beside `pricing_source`; a
   nullable `cost_usd` so `unknown` can never be summed as zero.
8. **Adopt three timeout clocks and a classified error taxonomy** — connect, time-to-first-token, and
   a per-chunk idle timeout with no total deadline on a stream; `Retry-After` honoured with a cap,
   full jitter, no retry after a partial stream, and distinct handling for context overflow,
   `model_not_found`, content-policy blocks and TLS failures.
9. **Make the embedding space a first-class versioned object** with a probed width, a per-space
   physical table, reads filtered to exactly one space, dual-write during migration, and a cutover
   gated on the existing 45-pair retrieval eval with one-statement rollback.
10. **Split the `models` kernel capability into transport / discovery / bound**, so a wrong endpoint
    leaves Settings reachable while everything that needs a model stays honestly down, with the
    kernel's blast radius naming the collateral.
11. **Move the credential to a `_FILE`-mounted secret**, read at use time, wrapped in a redacting
    type, and hand plugins a bound client — never a key. Where the proxy supports it, mint one virtual
    key per project stored with the existing `ConnectorCredential` encryption, and degrade cleanly to
    the shared key when it does not.
12. **Reconcile against the gateway's spend log nightly, never delegate to it**, and report drift as a
    first-class signal.

## What Aleph should avoid

1. **Do not treat "OpenAI-compatible" as a contract.** The dangerous divergences return HTTP 200:
   vLLM accepts `strict: true` and ignores it; Ollama accepts `tool_choice` and ignores it. Assume a
   capability is absent until probed, and write the agent loop to survive the weak case — validate
   every tool call's arguments client-side and treat a malformed one as a recoverable turn.
2. **Do not treat unknown as false, or unpriced as free.** Not for capability, not for context
   window, not for price. It is the mistake this package was written to fix and it is still live in
   the capability fields.
3. **Do not depend on LiteLLM proxy features.** The moment an operator points Aleph at bare Ollama,
   its fallbacks, budgets, virtual keys and spend logs vanish. Use them as optimisations, never as
   mechanism — and remember LiteLLM currently writes no spend row for streaming `/v1/responses`.
4. **Do not add the LiteLLM SDK in-process**, and do not adopt the official OpenAI Python SDK without
   budgeting the `httpx2` migration — SDK v3.0.0 (12 Aug 2026) dropped `httpx`, which Aleph pins at
   `0.28.1` in three packages, and it shipped 2.54.0 → 3.3.0 in six days.
5. **Do not commit a model list, a price list, or an embedding dimension.** A fetched, dated, cached
   catalog is discovery; a dict in a `.py` file is a committed table however it is labelled.
   `KNOWN_EMBEDDING_DIMS` and `EMBEDDING_DIM = 1024` are the two live violations.
6. **Do not let a catalog or a hint override a gateway-reported value**, and never let a catalog price
   a local endpoint. Precedence: probe (demotion only) > gateway > catalog > operator > unknown.
7. **Do not fan out probes without a bound**, and do not spend probe money without recording it. 415
   simultaneous billed completions is a rate-limit storm and an abuse flag, not a configuration step.
8. **Do not let one probe failure permanently demote a gateway-reported capability.** Mark it
   contested, cache transient failures for an hour, and show the operator the conflict.
9. **Do not silently downgrade to a lesser model, and do not silently re-bind.** Discovery produces
   proposals; humans confirm them; the ledger records the decision. A research system whose model
   changed under it cannot explain its own outputs.
10. **Do not retry a stream that already emitted tokens**, and do not put a total deadline on a
    streamed generation. The first re-bills the prompt and yields two partial answers; the second
    kills legitimate long reasoning outputs.
11. **Do not change the embedding model as a configuration edit**, and do not let autoconfigure fire a
    re-embed as a side effect of a settings save. Without a versioned space and a validated cutover
    you get an index mixing two embedding spaces, returning confidently ranked nonsense, raising no
    error anywhere.
12. **Do not put a plugin boundary on the per-token path.** Plugins act once per endpoint or once per
    request; SSE deltas pass straight through. And do not map plugins onto containers — a network hop
    per plugin is how a plugin architecture actually becomes slow.
13. **Do not let a plugin hold a raw credential or set an outbound auth header.** The transport
    chokepoint is also the credential boundary; an agent that can set an `Authorization` header is an
    agent that can exfiltrate.
14. **Do not add a second parser for an endpoint that already has one.** Two readers of `/v1/models`
    will drift, and the drift will surface as a binding that validates and fails at runtime.

---

## Sources

Design derived from, and all live facts sourced to,
`docs/research/openai-compatible-endpoints.md` and `docs/research/model-discovery.md` (both written
19 Aug 2026, versions verified live against PyPI, npm, the GitHub releases API and vendor endpoints
on that date), with the deployment shape from `docs/research/compose-deployment.md`.

Aleph code read at `bcc478a`: `packages/aleph-models/src/aleph_models/{client,discovery,pricing,
hints,profile,retry}.py`; `packages/aleph-core/src/aleph_core/schemas/model_profile.py`;
`packages/aleph-rks/src/aleph_rks/{embedding,models,retrieval}.py`;
`packages/aleph-runtime/src/aleph_runtime/capabilities.py`;
`packages/aleph-kernel/src/aleph_kernel/{spec,kernel,manifest}.py`;
`apps/api/src/aleph_api/routes/model_profile.py`; `apps/{api,workers}/src/*/settings.py`.

Reference implementations read as blueprints, not dependencies (all MIT):
`~/Documents/code/inspiration/{opencode,prime-agent,hermes-agent,deepseek-harness}`.
