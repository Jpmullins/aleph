# Architecture

Aleph is a multi-agent research environment built around three layers:

1. **RKS** (Raw Knowledge Store) — ingested sources, normalized text, chunks. Upstream of the wiki.
2. **Compiled Wiki** — the **primary retrieval surface** for both assistant and analyst. Wikilinked, revisioned, multi-agent-maintained, freshness-scored.
3. **A2UI workspace** — a 3-panel React UI whose right panel is five data-bound A2UI surface tabs (Wiki / Library / Notes / Hypotheses / Briefs).

## Monorepo layout

A `uv` workspace (Python 3.13, pyright strict) alongside a `pnpm` workspace (only `apps/web`). The package set below is the authoritative list from `pyproject.toml` `[tool.uv.workspace] members`.

```
apps/
  api/            FastAPI — request/response + SSE; owns Alembic; hosts the page-selector + AG-UI agent
  web/            React 19 + Vite + Tailwind + @a2ui/react + CopilotKit
  workers/        Arq workers — wiki agent, reviewers, builder, normalization, chunk+embed, curator, re-embed, native research loop
  code-runner/    Sandboxed, credential-less, network-partitioned Arq worker that executes agent-written Python
  copilot-runtime/  Node @copilotkit/runtime v2 bridge (:4000) — the Live chat → AG-UI boundary
packages/
  aleph-core           shared primitives, Pydantic schemas, UUIDv7. LEAF — imports nothing else.
  aleph-db             SQLAlchemy ORM + repositories + Alembic models
  aleph-security       auth, Principal, JWT, role gates, agent tokens
  aleph-observability  OTEL + Langfuse + structlog
  aleph-models         LiteLLMClient, pricing, ModelProfile resolver
  aleph-scholar        verified scholarship — Crossref/OpenAlex/Consensus, DOI verification. Pure-HTTP, zero LLM calls.
  aleph-rks            Raw Knowledge Store domain
  aleph-wiki           wiki compile / index / aliases / hand-edits / curator / freshness / HTML compiler
  aleph-assistant      chat orchestration, wiki retrieval router
  aleph-connectors     typed connector plugins (document / dataset_rows) — wired into the native research loop
  aleph-research       native deep-research LangGraph loop (plan→search→ingest→reflect→compose→synthesize)
  aleph-a2ui           A2UI catalog + Python SDK glue
  aleph-reviewer       MechanicalReviewer + EditorialReviewer + source-retraction service
  aleph-hypotheses     analyst-authored hypotheses
  aleph-datasets       Dataset / DatasetVersion / Observation
  aleph-artifacts      Builder agent, RenderedAssets, exporters
  aleph-notes         analyst notebook
  aleph-evals          eval runner, datasets, scorers, CI gates
```

Research runs natively in `aleph-research` (see `research-loop.md`); there is no separate research-subsystem package.

## The strict DAG

Dependencies flow **higher → lower only**. `aleph-core` is the leaf and imports nothing else. `aleph-api` and `aleph-workers` (and `aleph-code-runner`) depend on packages; packages never depend on apps; there are no cycles.

Foundational tier (leaf-ward): `aleph-core` → `aleph-db` / `aleph-security` / `aleph-observability` → `aleph-models`. `aleph-scholar` is pure-HTTP and carries **no workspace deps** (credentials + persistence injected as async callbacks). Domain tier: `aleph-rks`, `aleph-wiki`, `aleph-connectors`, `aleph-scholar` sit above the foundation; `aleph-research` composes `aleph-connectors` + `aleph-scholar` + `aleph-rks` + `aleph-wiki` + `aleph-models` + LangGraph. `aleph-assistant`, `aleph-reviewer`, `aleph-hypotheses`, `aleph-datasets`, `aleph-artifacts`, `aleph-notes`, `aleph-a2ui`, `aleph-evals` are the top domain packages the apps wire together.

## The load-bearing rules

Enforced by code review, the committed sweeps, and the eval gates — not aspirational.

1. **Wiki-first retrieval.** Primary path is `WikiIndex page-selector LLM → load pages + 1-hop wikilinks → answer composer`. Embeddings (`DocumentChunk` over pgvector) are used **only** for intra-source descent, never as first-line RAG. No "secret RAG" shortcut.
2. **All LLM traffic routes through the Insights LiteLLM gateway.** Non-agent code imports `LiteLLMClient` from `aleph-models`. Agent-framework code (CopilotKit / LangGraph / Deep Agents) MAY use `langchain_openai.ChatOpenAI` **only** pointed at the gateway (`base_url=LITELLM_BASE_URL`, `api_key=INSIGHTS_LITELLM_API_KEY`). No provider SDK is called directly.
3. **Agent → service is the only path to state.** Agents never write to Postgres or the asset store directly. They call typed `aleph-api` service methods; workers re-enter the API over HTTP (`ALEPH_API_INTERNAL_URL`) for mutations.
4. **Every mutation writes an Action Ledger event in the same transaction.** Hash-chained, append-only, no deletes. Integration tests assert ledger-event-count per mutation.
5. **Every LLM/tool/embed call writes a `ModelCall` + `CostLedgerEvent`** and is wrapped in an OTEL/Langfuse span. Cost is computed in `aleph_models.pricing` from rates learned at runtime — the gateway's own `/model/info` where the key may read it, the operator hints file otherwise. Every row records `pricing_source` (`gateway` = reported, `static` = asserted, `unknown` = unpriced) plus the rates used, so a cost stays re-derivable and an unpriced call is never a silent `$0`.
6. **Every row carries `project_id`** + `created_at`, `updated_at`, `created_by`, `access_scope`, optional `trace_id`, `ledger_event_id`. No global resources (only exception: `ModelProfile` templates).
7. **`ModelProfile` resolves capability → model; the gateway decides what those models are.** Call sites pass a `Capability` and the project's profile; `LiteLLMClient` resolves the binding. **Aleph ships no model list and no price list** — `aleph_models.discovery` reads what it can from the gateway, preferring `/model/info` (modes, context windows, capability flags, exact rates) and falling back to `/v1/models` (ids only) when the virtual key is restricted to `llm_api_routes`, which is the normal case. `aleph_models.hints` then fills unreported fields from an operator-editable file (`ALEPH_MODEL_HINTS_PATH`), never overriding the gateway and always labelled `static`. This populates the Settings picker and, via `POST /v1/projects/{id}/model-profile/autoconfigure`, the default bindings. Defaults are chosen by *requirements over metadata* (mode, context window, tool/vision support, price) — never by model name — and `autoconfigure` probes each model before binding it, because a gateway's list states configuration, not reachability. Priced models outrank unpriced ones; an unpriced binding is made only as a last resort and is reported. A capability no available model satisfies is left **unbound**. The named profiles `aleph-dev` / `aleph-production` remain as presets selected by `ALEPH_DEFAULT_MODEL_PROFILE`.
8. **A2UI surfaces are declarative; sandboxed artifacts are the only escape hatch.** *(amended)* Agents request components by name + props via the catalog. **No agent-emitted code executes in the app context.** Agent-written code runs only in the sandboxed, credential-less, network-partitioned `code_runner` worker; its outputs are versioned artifacts; interactive artifacts render only inside iframes with `sandbox` isolation (no same-origin, no network) whose `src` must be the asset streaming route. **No agent-emitted SQL.**

## Process boundaries

- `aleph-api` — synchronous HTTP + SSE, holds the user-identity boundary, owns Alembic, hosts the page-selector and the in-process AG-UI Deep Agent.
- `aleph-workers` — long-running agent jobs (LangGraph / Deep Agents), reviewers, normalization, the native research loop (`deep_research_job`), the curator, and wiki refresh. Holds short-lived agent tokens, dual-homes onto the platform network and `code-runner-net` to dispatch code jobs.
- `aleph-code-runner` — a separate compose service that executes agent-written Python in isolation: `cap_drop: [ALL]`, read-only rootfs, no DB/S3/LLM/token env, and an `internal: true` network whose only reachable peer is a dedicated `code-runner-redis`. See `workspace.md` and `security.md`.
- `aleph-copilot-runtime` — Node `@copilotkit/runtime` v2 service (:4000) bridging the React app to `aleph-api`'s AG-UI Deep Agent endpoint; where A2UI tool injection + the inline catalog live.

There is no separate research-subsystem process — research is an in-process worker loop.

## Living invariants (committed CI sweeps)

Four scripts run in CI (`.github/workflows/ci.yml`) and fail the build on drift:

- `scripts/check-no-self-fetch.sh` — greps `apps/web/src/a2ui/components` for `useQuery`/`refetchInterval`/`EventSource`/`fetch(`/`api.get|post|…`; the allowlist is empty. Right-panel components render only from bound props.
- `scripts/check-catalog-roster.sh` — every catalog component has both a renderer and a producer; the `catalog.py` ⟷ `catalog.ts` rosters agree; deleted cards (`MapCard`/`GraphCard`/`NotebookCellCard`) appear nowhere.
- `scripts/check-route-reachability.sh` — every mounted `include_router` is reached by a real web/agent/script/test caller (small documented allowlist for public/external routers).
- `scripts/check-catalog-generated.sh` — the A2UI catalog has one editable copy (`catalog.json`); the web and copilot-runtime views are generated from it by `scripts/gen_catalog.py`. Catching drift is weaker than making it impossible: the three hand-maintained copies disagreed on `ClaimCard.confidence` in ways no test noticed until an agent hit them.
- `scripts/check-graph-state-keys.sh` — every key a LangGraph node writes is declared on its state `TypedDict`. Undeclared writes are **discarded silently**: the write vanishes, the reader's `state.get(k, [])` returns empty, and the feature is inert while every step reports success. Four shipped defects had this shape.
- `scripts/check-docs-drift.sh` + `scripts/check-claude-commands.sh` — WP-7 doc guards: stale deleted-subsystem / object-store references appear only under `docs/archive/` + the impl-log, and every command/script/file/compose-service named in CLAUDE.md exists in the repo.

## Auth modes

`ALEPH_AUTH_MODE` selects the user-auth path: `local` (compose default — OIDC skipped, a fixed `dev@aleph.local` principal, JIT-provisioned) or `oidc` (full JWT/JWKS verification). Agent tokens (HS256, internal) are always verified. See `security.md`.
