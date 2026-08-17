# Architecture

What exists today. For where it is going, see [`belief-engine.md`](belief-engine.md); for why,
[`decisions.md`](decisions.md); for the per-part checks, [`acceptance.md`](acceptance.md).

## Processes

| service | role |
|---|---|
| `aleph-api` | FastAPI. Synchronous HTTP + SSE. Holds the user-identity boundary. Owns Alembic. Hosts the in-process agent. Boots on the kernel. |
| `aleph-workers` | arq workers — ingest, normalization, chunk+embed, the research loop, reviewers. Boots on the same kernel manifest. Holds short-lived agent tokens; re-enters the API over HTTP for every mutation. Dual-homed onto `code-runner-net` to dispatch code jobs. |
| `aleph-code-runner` | Executes agent-written Python in isolation: `cap_drop: [ALL]`, read-only rootfs, no DB/S3/LLM credentials in env, on an `internal: true` network whose only peer is a dedicated `code-runner-redis`. |
| `aleph-copilot-runtime` | Node bridge (:4000) between the React app and the API's agent endpoint. One file plus the generated catalog. |
| `aleph-web` | React 19 + Vite + Tailwind. |

Supporting: `postgres` (pgvector), `redis`, `code-runner-redis`, and the Langfuse stack
(`langfuse`, `langfuse-worker`, `clickhouse`, `langfuse-redis`, `langfuse-minio`) plus
`otel-collector`. `minio` is opt-in under the `s3` profile; a chat+embedding gateway for local models
is opt-in under `local-llm` (see [`operations.md`](operations.md)).

## Boot

Both Python processes boot on the kernel (`packages/aleph-kernel`). `apps/api/.../lifespan.py` and
`apps/workers/.../arq.py` each load a boot manifest (`aleph.toml`), mount it onto a `Kernel`, and
`boot()`. The shared services — observability, database, HTTP clients, redis, JWKS, models, assets,
scholar, realtime, agent store, arq pools — are declared once as kernel capabilities in
`packages/aleph-runtime`, which is the composition root for both processes.

Two properties come from that, rather than from discipline:

- **Every capability carries a probe that exercises its real read path.** A capability that cannot
  answer a live query does not come up.
- **Teardown is an `EffectScope` unwound LIFO.** Each inverse is authored next to the thing it undoes,
  every inverse runs even when one raises, and a partially-completed boot unwinds what it built. The
  previous hand-ordered `finally` leaked the engine pool, the Redis connection and the HTTP clients
  whenever a constructor or an `aclose()` raised.

## Data flow

```
source (upload | connector | research loop)
   → RKS: normalize → defang → chunk → embed     [aleph-rks]
   → scholarly verification: DOI, retraction,     [aleph-scholar]
     bidirectional citation-graph expansion
   → knowledge layer                              [TRANSITIONING: aleph-wiki → the Claim Spine]
   → retrieval → answer / artifact                [aleph-assistant]
```

Every mutation along that path writes an `ActionLedgerEvent` in the same transaction. Every LLM and
embedding call is routed through the LiteLLM gateway by `aleph-models` and should produce a
`ModelCall` + `CostLedgerEvent` — the agent path has a known hole here (see CLAUDE.md).

## The knowledge layer, currently

Two representations coexist during the transition:

- **RKS** (`aleph-rks`) — sources, normalized text, chunks, embeddings. Healthy. This is the ground
  truth the knowledge layer is derived from, and it is what makes a rebuild possible.
- **Wiki** (`aleph-wiki`) — LLM-compiled markdown pages. **Legacy under removal.** Its index now
  covers page bodies (see Retrieval), so it is no longer broken — it is superseded. Nothing new is
  built on it.

The replacement inverts the ownership: claims become durable and independently revisable, and prose
becomes a render. `packages/aleph-belief` holds the patch contract, the trust lattice and the
deterministic reconciler; `aleph_wiki.belief_service` is the Claim Spine's write path, sitting in the
legacy package because the claim tables do (`WikiClaim`, `Citation`, `ClaimEdge` in
`aleph_wiki.models`). What is still missing is the extractor that turns an ingested source into claim
drafts — that, not appetite, is what blocks deleting the wiki (`acceptance.md` §E).

## Retrieval

The wiki-first path — FTS over `wiki_index` → an LLM page-selector → one hop of wikilink expansion →
a composer call — is being removed. Its three known defects were fixed rather than inherited:
`wiki_index` now carries `body_text` with weighted `ts_rank` (title A, summary+aliases B, body C), the
query gate ORs terms instead of ANDing them, and link expansion filters on `src_revision_id` so it no
longer walks every historical revision. The page-selector hop and `Capability.PAGE_SELECTION` go with
the wiki.

What replaces it is `aleph_rks.retrieval.search_corpus`: corpus-wide hybrid search over
`DocumentChunk`, fusing a dense (pgvector cosine) and a lexical (`ts_rank`) ranking with **RRF at
k=60**. Both legs over-fetch so fusion has something to disagree about; `descend_into_source` keeps
the single-source predicate for intra-document descent.

The former prohibition on vector-first retrieval is lifted — it was the constraint that left the wiki
with no semantic entry point at all. The dense leg is measured, not assumed: on the 45-pair labelled
set in `packages/aleph-evals/datasets/retrieval/`, recall@1 is 0.91 hybrid against 0.60 lexical-only,
and almost all of that margin is on paraphrased questions. Reproduce with
`uv run python -m aleph_evals.retrieval_eval`.

## Model routing

Call sites pass a `Capability` (`synthesis`, `extraction`, `classification`, `embedding`, `rerank`,
`vision`, `code`, `judge`) plus the project's `ModelProfile`; `LiteLLMClient` resolves the binding.
Two named profiles — `aleph-dev` and `aleph-production` — are presets selected by
`ALEPH_DEFAULT_MODEL_PROFILE`. `Capability.PAGE_SELECTION` is being removed with the page-selector.

**Aleph ships no model list and no price list.** The gateway decides what models exist:

- `aleph_models.discovery` prefers the gateway's `/model/info` (modes, context windows, capability
  flags, exact rates) and falls back to `/v1/models` (ids only) when the virtual key is restricted to
  `llm_api_routes`, which is the normal case.
- `aleph_models.hints` fills unreported fields from an operator-editable file
  (`ALEPH_MODEL_HINTS_PATH`), never overriding the gateway.
- `POST /v1/projects/{id}/model-profile/autoconfigure` picks defaults by *requirements over metadata*
  — mode, context window, tool/vision support, price — never by model name, and **probes** each model
  before binding it, because a gateway's list states configuration, not reachability. Priced models
  outrank unpriced ones; a capability no available model satisfies is left **unbound** rather than
  bound to a guess.

Cost is computed in `aleph_models.pricing` (cache-discount aware) from those learned rates. Every
priced row records `pricing_source` — `gateway` (reported), `static` (asserted from hints), `unknown`
(unpriced) — plus the rates used, so a cost stays re-derivable and an unpriced call is never a silent
`$0`. The predecessor hardcoded one gateway's names and prices; against a second, Bedrock-backed
gateway not one name matched and the recorded numbers were roughly 3x the real billing.

## Workspace UI

The shell is `rail │ reading region │ assistant dock`: `Rail` (project objects and navigation),
`ContextBar` + `PipelineStrip` above the stage (project state and the corpus's per-stage source
counts, fed by `GET /v1/projects/{id}/pipeline`), `ReadingRegion` (the stage) and `AssistantDock`
(chat, collapsible to nothing and giving the space back).

The stage holds **panes, not tabs.** Up to `MAX_PANES` (3) data-bound A2UI surfaces tile
left-to-right, each one a wire `surfaceId`, so a claim can sit beside the source that grounds it.
Wiki / Library / Notes / Hypotheses / Briefs are pane kinds now rather than a tab strip, and the
stream serves two more that the rail does not open: `artifacts`, and `grounding`, parameterised by
a claim id because it is opened *from* a claim (`_PANE_KINDS` in `routes/surfaces.py`). A single
slot structurally cannot do comparison, which is the work.

Surfaces are built server-side and pushed as `updateDataModel` deltas over **one** SSE connection
for the whole region, woken by Postgres `LISTEN`/`NOTIFY`. Multiplexing is not just cheaper than a
connection per pane (which hits the browser's ~6-per-origin HTTP/1.1 cap at four): the server
stamps one monotonic `seq` per connection, so every pane shares a single total order and two panes
cannot render mutually inconsistent states with nothing detecting it. A pane owns no transport at
all — it renders one `surfaceId` out of that stream. `GroundingSurface` renders a claim's evidence
chain down to the chunk span.

The A2UI catalog has exactly one editable copy: `packages/aleph-a2ui/src/aleph_a2ui/catalog.json`.
`apps/web/src/a2ui/catalog.ts` and `apps/copilot-runtime/src/catalog.generated.ts` are generated from
it by `scripts/gen_catalog.py` and checked by `scripts/check-catalog-generated.sh`. Three
hand-maintained copies previously disagreed about `ClaimCard.confidence` — the agent-facing one
offered a value nothing recognised and omitted the retracted state entirely.

Agents request components by name and props from that catalog — no agent-emitted code executes in the
app context. Agent-written code runs only in `code-runner`, producing versioned artifacts rendered in
`sandbox` iframes whose `src` must be the authenticated asset streaming route.

## Storage

An `AssetStore` protocol with two backends: local filesystem (default, `data/assets`) and
S3-compatible (opt-in via the `s3` compose profile). One authenticated streaming route serves assets
under a sandboxing CSP.

---

# Security

## Auth modes

`ALEPH_AUTH_MODE` selects the path; the frontend mirrors it with `VITE_AUTH_MODE`.

- **`local`** (compose default) — JWT verification skipped; every non-public request maps to a fixed
  `dev@aleph.local` principal, JIT-provisioned and ledgered as `user.create`. Agent tokens (HS256,
  internal) are still verified. No IdP runs locally.
- **`oidc`** — full JWT/JWKS verification against any OIDC IdP (Cognito, Auth0, Authentik, Keycloak,
  ALB OIDC), via `ALEPH_AUTH_ISSUER`, `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL`. Dormant in local
  mode but kept intact, so deploying is a config flip.

Only `/healthz`, `/readyz`, `/docs`, `/redoc`, `/openapi.json` and `/static/` bypass the middleware.
Project scoping (`project_scope_dep`) requires membership and raises **404**, not 403, on a project
the caller is not in, so non-members cannot confirm one exists. A soft-deleted project answers
**409** on writes — it exists, and saying so is what tells a user to restore it.

## Agent tokens and the agent endpoint

Internal service-to-service auth is short-lived HS256 agent tokens signed with
`ALEPH_AGENT_TOKEN_SECRET`. Code that self-calls the API (worker jobs, `copilot_agent`, subagents,
`a2ui_handlers`) mints one scoped to the acting project/agent-run with `mint_agent_token(...)`;
`verify_agent_token` checks it. External callers obtain one from `POST /v1/agent-tokens`. There is no
hardcoded `Bearer local-dev` in server code — `apps/api/tests/unit/test_self_call_tokens.py` greps for
it and fails on a reintroduction; the only local-mode sentinel is the documented frontend one in
`apps/web/src/lib/auth.ts`.

The AG-UI agent endpoint used to sit on a middleware skip list, on a comment promising the handler
verified callers. It did not — `copilotkit_endpoint.py` contained no auth code — while the agent's
tools took their project scope from a client-supplied `thread_id` (`proj:<uuid>:<thread>`). Any caller
who could reach `:8000`, or the CORS-open bridge on `:4000` in front of it, could drive the agent's
**write** tools against an arbitrary project, in both auth modes. Three things close it, and all three
are load-bearing:

1. `_SELF_AUTH_PREFIXES` is empty. The endpoint authenticates like every other route.
   (`test_copilotkit_auth.py`, which also asserts the tuple stays empty.)
2. The HTTP boundary refuses a request naming a project the caller is not a member of — or one
   outside an agent token's signed binding — before the graph starts and before a token is spent.
   `middleware/agent_scope.py` walks the whole JSON body for thread ids and explicit project ids at
   any depth, all-or-nothing so an unauthorized project cannot ride alongside an authorized one, and
   `test_thread_parsers_agree` pins its parser to the agent's own so the two cannot drift into a gap.
3. Inside the graph, tools re-authorize against the task-local principal bound by the middleware
   (`test_agent_project_authorization.py`).

An agent token's signed `project_id` is no longer discarded: it rides on the `Principal`, and
`_assert_credential_scope` refuses a token presented against a different project — on stream routes
too (`test_agent_token_project_scope.py`).

## ConnectorCredential encryption and Consensus OAuth

`ConnectorCredential` stores third-party secrets encrypted at rest: `libsodium-sealed` (a sealed box
with a per-project keypair) for local/dev, with a `kms-aes-gcm` scheme for production. Plaintext is
decrypted only server-side inside the owning service, never logged, never in ledger payloads, and
never returned by any route. All writes go through `ConnectorCredentialService`, ledgered as
`connector_credential.create|update|delete`. Every route on `connector_credentials.py` — list, upsert,
delete, rotate — is OWNER-gated, and the list route derives only a `status` string from the decrypted
blob; the plaintext never leaves the server.

The **Consensus** credential is an OAuth blob bootstrapped by `scripts/connect-consensus.py`
(RFC 9728/8414/7591 discovery + PKCE loopback, so it needs a human at a browser). Scholar refreshes
the access token server-side under a per-project Redis lock; a rotated refresh token re-upserts the
blob. An authoritative refresh rejection (HTTP 400/401) yields a queryable `reconnect_required`
status rather than a 500.

## code-runner isolation

Agent-written code executes **only** in `aleph-code-runner`, never in a credentialed process:

- **Network-partitioned.** Its only network is `code-runner-net` (`internal: true` → no NAT, no
  internet), whose only reachable peer is a **dedicated `code-runner-redis`** — not the platform
  Redis, which carries agent tokens as job args, privileged queues and the LISTEN/NOTIFY streams. The
  dedicated bus carries only the code-job payload.
- **No credentials.** No `DATABASE_URL`, `ALEPH_S3_*`, `LITELLM_*` or `ALEPH_AGENT_TOKEN_SECRET`; no
  asset mount. Its entire environment is the Redis URL.
- **Locked down.** `cap_drop: [ALL]`, `read_only: true` rootfs plus a 256 MB tmpfs scratch,
  `no-new-privileges`, non-root, `pids_limit: 128`, 1 GB `mem_limit`/`memswap_limit`.
- **A second layer inside the container.** `aleph_code_runner.executor` never runs agent code in the
  worker process. The child runs under `python -I` (env vars and user site ignored, and the script
  dir kept off `sys.path`) and, before the agent's code executes, installs a socket guard that makes
  every network primitive raise, best-effort `unshare`s an empty network namespace, and sets
  `RLIMIT_CPU` / `RLIMIT_FSIZE` under a wall-clock timeout.
- **Worst case.** A full escape yields CPU/memory inside the cgroup caps, the agent's own submitted
  code, and the ephemeral code-job bus — never token capture or privileged-job injection. The
  documented residual: a raw-`ctypes` syscall could bypass the Python-level socket guard and reach
  `code-runner-redis`, which carries no secrets.

`aleph-workers` dual-homes onto both networks: it dispatches and awaits code jobs and does the
privileged persistence, turning returned bytes into versioned artifacts.

## CSP-sandboxed asset serving

One authenticated route streams stored bytes:
`GET /v1/projects/{pid}/assets/{kind}/{id}`. It sends `X-Content-Type-Options: nosniff` and, on every
non-PDF response, a CSP `sandbox` — `sandbox allow-scripts` for interactive HTML artifact versions,
where scripts then run in an opaque origin with no access to the API origin's ambient auth. PDFs are
exempt because Chromium's viewer refuses to render in a sandboxed document. Because the header is
server-side, it also covers a direct URL open, not just the iframe. This matters because in `local`
mode every same-origin request carries ambient auth.

The client half is enforced, not conventional: `isSandboxedAssetSrc` in
`apps/web/src/a2ui/components/_shared.tsx` admits only the two principal-boundary, CSP-sandboxed
paths — `/v1/projects/{id}/assets/{rendered|artifact-version}/{id}` and the compiled-wiki
`/v1/projects/{id}/wiki/pages/{id}/html` — and `HtmlDocCard`, `HtmlFrameCard` and `ImageCard` each
refuse to mount, rendering a visible refusal notice, on anything else. A `data:` URI, an external
origin or an arbitrary agent-supplied URL therefore never becomes a frame or an `<img>`.

## Ledger hash chain

Every mutation writes an `ActionLedgerEvent` in the same transaction: hash-chained, append-only, with
Postgres `ledger_no_update` / `ledger_no_delete` triggers that raise on any attempt to change history.
`verify_project_chain` walks `prev_event_id` from the chain head rather than following timestamp
order, so a tampered `chain_hash`, a dangling link or an ambiguous head fails verification even if the
rows are reordered.

## Untrusted text at the boundary

Ingested documents reach the model's context, and under the research loop they reach it with no human
in between. `aleph_rks.normalization` runs `aleph_core.grounding.defang` over normalized markdown —
stripping invisible/bidi control characters and folding U+2028/U+2029 — and flags the source
`defanged_invisible_characters`, so a document cannot show a reviewer one thing and the model another.

## Known gaps

Both are the same class, both are out of scope until OIDC deployment is taken up as a whole, and
**`local` mode — the only deployed mode — is unaffected by either.**

- **The runtime bridge does not forward the caller's credential.** `copilot-runtime/src/server.ts`
  builds `new HttpAgent({ url })` with no headers, so in `oidc` mode the chat path now correctly
  demands a credential it never receives. Closing it means per-request header propagation from
  browser → runtime → API.
- **SSE cannot carry a bearer token.** `EventSource` cannot set an `Authorization` header, so the SSE
  streams (agent-events, surfaces, assistant, `changes`) and the `<iframe>`-consumed asset route have
  no token transport in `oidc` mode. Closing it means a short-lived query-param or cookie exchange for
  stream endpoints.
