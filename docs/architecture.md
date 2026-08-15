# Architecture

What exists today. For where it is going, see [`belief-engine.md`](belief-engine.md); for why,
[`decisions.md`](decisions.md).

## Processes

| service | role |
|---|---|
| `aleph-api` | FastAPI. Synchronous HTTP + SSE. Holds the user-identity boundary. Owns Alembic. Hosts the in-process agent. |
| `aleph-workers` | arq workers — ingest, normalization, chunk+embed, the research loop, reviewers. Holds short-lived agent tokens; re-enters the API over HTTP for every mutation. Dual-homed onto `code-runner-net` to dispatch code jobs. |
| `aleph-code-runner` | Executes agent-written Python in isolation: `cap_drop: [ALL]`, read-only rootfs, no DB/S3/LLM credentials in env, on an `internal: true` network whose only peer is a dedicated `code-runner-redis`. |
| `aleph-copilot-runtime` | Node bridge (:4000) between the React app and the API's agent endpoint. One file. |
| `aleph-web` | React 19 + Vite + Tailwind. |

Supporting: `postgres` (pgvector), `redis`, `code-runner-redis`, and the Langfuse stack
(`langfuse`, `langfuse-worker`, `clickhouse`, `langfuse-redis`, `langfuse-minio`) plus
`otel-collector`. `minio` is opt-in under the `s3` profile.

## Data flow

```
source (upload | connector | research loop)
   → RKS: normalize → chunk → embed            [aleph-rks]
   → scholarly verification: DOI, retraction,   [aleph-scholar]
     bidirectional citation-graph expansion
   → knowledge layer                            [TRANSITIONING: aleph-wiki → aleph-belief]
   → retrieval → answer / artifact              [aleph-assistant]
```

Every mutation along that path writes an `ActionLedgerEvent` in the same transaction. Every LLM and
embedding call is routed through the LiteLLM gateway by `aleph-models` and should produce a
`ModelCall` + `CostLedgerEvent` — the agent path has a known hole here (see CLAUDE.md).

## The knowledge layer, currently

Two representations coexist during the transition:

- **RKS** (`aleph-rks`) — sources, normalized text, chunks, embeddings. Healthy. This is the ground
  truth the knowledge layer is derived from, and it is what makes a rebuild possible.
- **Wiki** (`aleph-wiki`) — LLM-compiled markdown pages. **Legacy under removal.** Its retrieval
  index covers only page titles and summaries, which is why it does not work.

The replacement inverts the ownership: claims become durable and independently revisable, and prose
becomes a render. `packages/aleph-belief` holds the patch contract and trust lattice; the rest is
being built.

## Retrieval

The path in use today is wiki-first: FTS over `wiki_index` → an LLM page-selector → one hop of
wikilink expansion → a composer call. It is being removed wholesale — the FTS index is body-blind,
the query gate ANDs every term, and the expansion reads links from every historical revision.

What replaces it already exists in the repo: `aleph-rks/retrieval.py` implements a hybrid
`0.6·cosine + 0.4·ts_rank` ranker, currently constrained to a single source by one predicate. Removing
that predicate makes it corpus-wide.

Embeddings (`DocumentChunk` over pgvector) are used for semantic recall. The former prohibition on
vector-first retrieval is lifted — it was the constraint that left the wiki with no semantic entry
point at all.

## Model routing

Call sites pass a `Capability` (`synthesis`, `extraction`, `classification`, `embedding`, `rerank`,
`vision`, `code`, `judge`) plus the project's `ModelProfile`; `LiteLLMClient` resolves the binding.
Two named profiles — `aleph-dev` and `aleph-production` — selected by `ALEPH_DEFAULT_MODEL_PROFILE`.
`Capability.PAGE_SELECTION` is being removed with the page-selector.

Cost is computed in `aleph_models.pricing`, cache-discount aware.

## Auth

`ALEPH_AUTH_MODE` selects the path:

- **`local`** (compose default) — JWT verification skipped; every non-public request maps to a fixed
  `dev@aleph.local` principal, JIT-provisioned. Agent tokens (HS256, internal) are still verified.
- **`oidc`** — full JWT/JWKS verification against any OIDC IdP, via `ALEPH_AUTH_ISSUER`,
  `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL`.

The frontend mirrors this with `VITE_AUTH_MODE`. Two known gaps: SSE cannot attach a bearer header
(no impact in local mode), and **the agent endpoint sits on the middleware skip list and derives
project scope from client-supplied state** — every other route is correctly gated.

## Workspace UI

Three panels; the right panel is five data-bound A2UI surface tabs. Surfaces are built server-side and
pushed as `updateDataModel` deltas over an SSE stream woken by Postgres `LISTEN`/`NOTIFY`.

Agents request components by name and props from a catalog — no agent-emitted code executes in the app
context. Agent-written code runs only in `code-runner`, producing versioned artifacts rendered in
`sandbox` iframes whose `src` must be the authenticated asset streaming route.

## Storage

An `AssetStore` protocol with two backends: local filesystem (default, `data/assets`) and
S3-compatible (opt-in via the `s3` compose profile). One authenticated streaming route serves assets
under a sandboxing CSP.
