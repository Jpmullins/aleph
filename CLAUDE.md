# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Aleph is a multi-agent research environment built around three layers:
1. **RKS** (Raw Knowledge Store) — ingested sources, normalized text, chunks. Upstream of the wiki.
2. **Compiled Wiki** — the **primary retrieval surface** for both assistant and analyst. Wikilinked, revisioned, multi-agent-maintained, freshness-scored, retraction-aware.
3. **A2UI workspace** — a 3-panel UI whose right panel is five **data-bound** A2UI surface tabs (Wiki / Library / Notes / Hypotheses / Briefs). The **Library** tab shows ingested **Sources** — raw PDFs/webpages/docs, viewable via the authenticated streaming route — alongside built **Artifacts**.

Research runs as a **native, in-process worker loop** (`aleph-research` → `deep_research_job`: plan → search → ingest → reflect → compose → the existing `SynthesisWorkflow` → a pending proposal in Briefs). Scholarship is verified through `aleph-scholar` (Crossref/OpenAlex/Consensus, tri-state DOI verification, retraction detection). Right-panel surfaces are server-built and pushed as `updateDataModel` deltas over an SSE stream woken by Postgres LISTEN/NOTIFY. Agent-written code executes only in a sandboxed, credential-less, network-partitioned `code_runner`, producing versioned artifacts rendered in `sandbox` iframes.

The canonical record of what shipped is `docs/implementation-log.md` (append-only; entries §WP-1..§WP-6 + earlier increments). The living, verified system is described by the seven top-level docs — see the Docs map below.

## Common commands

```bash
# First-time setup
cp deploy/compose/.env.example deploy/compose/.env   # then edit secrets
./scripts/bootstrap-local.sh                          # boot full stack

# Install deps
uv sync --all-packages --all-extras                   # Python (MUST be --all-packages: installs every workspace member)
pnpm -C apps/web install                              # JS (only apps/web is in pnpm workspace)

# Lint / format / typecheck
uv run ruff check .
uv run ruff format --check .
uv run pyright
pnpm -C apps/web typecheck
pnpm -C apps/web lint

# Tests
uv run pytest -m "not integration" -q                 # unit
uv run pytest -m integration -q                       # integration (needs compose stack + migrations)
uv run pytest path/to/test_file.py::test_name         # single test

# Evals (gate that CI runs)
uv run python -m aleph_evals --datasets all --gate strict

# Migrations (Alembic lives under apps/api/alembic)
cd apps/api && uv run alembic upgrade head
cd apps/api && uv run alembic check                   # CI asserts no model drift
cd apps/api && uv run alembic revision -m "<slug>" --autogenerate

# Web dev
pnpm -C apps/web dev                                  # vite dev server on :5173
pnpm -C apps/web build                                # tsc --noEmit && vite build

# Local services
docker compose -f deploy/compose/docker-compose.yml up -d
docker compose -f deploy/compose/docker-compose.yml logs -f aleph-api
docker compose -f deploy/compose/docker-compose.yml down

# Gateway sanity check
./scripts/verify-gateway.sh
```

Endpoints after bootstrap: web `:5173`, api `:8000`, copilot-runtime `:4000`, Langfuse `:3000`. The object-store console `:9001` is available **only** under the opt-in `s3` profile (`docker compose --profile s3 up -d`) — the default asset backend is the local filesystem.

## Architecture

Monorepo: `uv` workspace (Python 3.13, pyright strict) + `pnpm` workspace (only contains `apps/web`).

```
apps/
  api/            FastAPI — request/response + SSE; hosts WikiIndex page-selector + AG-UI agent; owns Alembic
  web/            React 19 + Vite + Tailwind + @a2ui/react + CopilotKit
  workers/        Arq workers — wiki agent, reviewers, builder, normalization, chunk+embed, curator, re-embed, native research loop, wiki refresh
  code-runner/    Sandboxed, credential-less, network-partitioned Arq worker that executes agent-written Python
  copilot-runtime/  Node @copilotkit/runtime v2 bridge (:4000) — the Live chat → AG-UI boundary
packages/
  aleph-core              shared primitives, Pydantic schemas, UUIDv7. LEAF — imports nothing else.
  aleph-db                SQLAlchemy ORM + repositories + Alembic models
  aleph-security          auth, Principal, JWT, role gates, agent tokens
  aleph-observability     OTEL + Langfuse + structlog
  aleph-models            LiteLLMClient, pricing, ModelProfile resolver
  aleph-scholar           verified scholarship — Crossref/OpenAlex/Consensus, tri-state DOI verification. Pure-HTTP, ZERO LLM calls.
  aleph-rks               Raw Knowledge Store domain
  aleph-wiki              wiki compile / index / aliases / hand-edits / curator / freshness / deterministic HTML compiler
  aleph-assistant         chat orchestration, wiki retrieval router
  aleph-connectors        typed connector plugins (document / dataset_rows) — registered into and driven by the native research loop
  aleph-research          native deep-research LangGraph loop (plan→search→ingest→reflect→compose→synthesize)
  aleph-a2ui              A2UI catalog + Python SDK glue
  aleph-reviewer          MechanicalReviewer + EditorialReviewer + source-retraction/blast-radius service
  aleph-hypotheses        analyst-authored hypotheses
  aleph-datasets          Dataset / DatasetVersion / Observation
  aleph-artifacts         Builder agent, RenderedAssets, exporters
  aleph-notes             analyst notebook
  aleph-evals             eval runner, datasets, scorers, CI gates
```

**Strict DAG, higher → lower only.** `aleph-core` is the leaf. `aleph-api`/`aleph-workers`/`aleph-code-runner` depend on packages; packages never depend on apps; no cycles. `aleph-scholar` is pure-HTTP and carries no workspace deps. `aleph-research` composes `aleph-connectors` + `aleph-scholar` + `aleph-rks` + `aleph-wiki` + `aleph-models` + LangGraph.

### The load-bearing rules

Enforced by code review, the committed sweeps, and the eval gates — not aspirational.

1. **Wiki-first retrieval.** Primary path is `WikiIndex page-selector LLM → load pages + 1-hop wikilinks → answer composer`. Embeddings (`DocumentChunk` over pgvector) are used **only** for intra-source descent, never as first-line RAG. Don't add a "secret RAG" shortcut.
2. **All LLM traffic routes through the Insights LiteLLM gateway.** Non-agent code imports `LiteLLMClient` from `aleph-models`. Agent-framework code (CopilotKit / LangGraph / Deep Agents) MAY use `langchain_openai.ChatOpenAI` **only** when pointed at the gateway (`base_url=LITELLM_BASE_URL`, `api_key=INSIGHTS_LITELLM_API_KEY`). **No provider SDK is called directly.** `apps/api/src/aleph_api/copilot_cost_callback.py` (`AgentCostCallbackHandler`) attaches to the orchestrator + every subagent model, writing `ModelCall`+`CostLedgerEvent` per call (requires `stream_usage=True`).
3. **Agent → service is the only path to state.** Agents (Live orchestrator, subagents, the research loop) never write to Postgres or the asset store directly. They call typed `aleph-api` service methods; workers re-enter the API over HTTP (`ALEPH_API_INTERNAL_URL`) with a minted agent token for every mutation.
4. **Every mutation writes an Action Ledger event in the same transaction.** Hash-chained, append-only, no deletes. Integration tests assert ledger-event-count per mutation.
5. **Every LLM/tool/embed call writes a `ModelCall` + `CostLedgerEvent`** and is wrapped in an OTEL/Langfuse span. Cost is computed in `aleph_models.pricing` (cache-discount-aware).
6. **Every row carries `project_id`** + `created_at`, `updated_at`, `created_by`, `access_scope`, optional `trace_id`, `ledger_event_id`. No global resources (only exception: `ModelProfile` templates).
7. **`ModelProfile` resolves capability → model.** Call sites pass a `Capability` (`synthesis`, `extraction`, `page_selection`, `classification`, `embedding`, `rerank`, `vision`, `code`, `judge`) and the project's profile; `LiteLLMClient` resolves the binding. Two named profiles: `aleph-dev` (Sonnet/Haiku) and `aleph-production` (Opus/Sonnet), selected by `ALEPH_DEFAULT_MODEL_PROFILE`.
8. **A2UI surfaces are declarative; sandboxed artifacts are the only escape hatch.** *(amended)* Agents request components by name + props via the catalog. **No agent-emitted code executes in the app context.** Agent-written code runs only in the sandboxed, credential-less, network-partitioned `code_runner` worker; its outputs are versioned artifacts; interactive artifacts render only inside iframes with `sandbox` isolation (no same-origin, no network) whose `src` must be the asset streaming route. **No agent-emitted SQL.**

### Living invariants (committed CI sweeps)

CI (`.github/workflows/ci.yml`) fails the build on drift in any of:
- `scripts/check-no-self-fetch.sh` — no `useQuery`/`refetchInterval`/`EventSource`/`fetch(`/`api.*` inside `apps/web/src/a2ui/components` (empty allowlist).
- `scripts/check-catalog-roster.sh` — every catalog component has a renderer + a producer; `catalog.py` ⟷ `catalog.ts` agree; deleted cards (`MapCard`/`GraphCard`/`NotebookCellCard`) appear nowhere.
- `scripts/check-route-reachability.sh` — every mounted router is reached by a real web/agent/script/test caller.
- `scripts/check-docs-drift.sh` — stale deleted-subsystem / object-store references appear only under `docs/archive/` + `docs/implementation-log.md`.
- `scripts/check-claude-commands.sh` — every command/script/file/compose-service named in this file exists.

### Process boundaries

- `aleph-api` — synchronous HTTP + SSE, holds the user-identity boundary, owns Alembic, hosts the page-selector and the in-process AG-UI Deep Agent.
- `aleph-workers` — long-running agent jobs (LangGraph / Deep Agents), reviewers, normalization, the native research loop (`deep_research_job`), the curator, wiki refresh. Holds short-lived agent tokens. Dual-homes onto the platform network + `code-runner-net` to dispatch code jobs. `POST /synthesize` enqueues `deep_research_job`; its output is consumed by the unchanged `SynthesisWorkflow` → a proposal in Briefs, never published directly.
- `aleph-code-runner` — a separate compose service executing agent Python in isolation: `cap_drop: [ALL]`, read-only rootfs, no DB/S3/LLM/token env, an `internal: true` network whose only peer is a dedicated `code-runner-redis`. See `docs/security.md`.
- `aleph-copilot-runtime` — Node `@copilotkit/runtime` v2 service (:4000) bridging the React app to `aleph-api`'s AG-UI Deep Agent endpoint; where A2UI tool injection + the inline catalog live.

There is no separate research-subsystem process.

### Auth modes

`ALEPH_AUTH_MODE` selects the user-auth path:
- `local` (compose default) — JWT verification skipped; every non-public request maps to a fixed `dev@aleph.local` principal, JIT-provisioned on first sight. Agent tokens (HS256, internal) still verified. No IdP service runs locally.
- `oidc` — full JWT/JWKS verification against any OIDC IdP (Cognito, Auth0, Authentik, Keycloak, ALB OIDC). Three env vars: `ALEPH_AUTH_ISSUER`, `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL`.

The OIDC path is dormant in local mode but kept intact so deploy is a config flip. Frontend mirrors via `VITE_AUTH_MODE`. The SSE×OIDC token-transport gap (EventSource can't attach a bearer header; local mode unaffected) is a documented out-of-scope gap — see `docs/security.md`.

## Engineering rules

Hard, enforced by CI:

- **No placeholder code in production paths.** CI greps for `TODO|FIXME|NotImplementedError` outside `tests/`.
- **Ship scope in final production form.** No "stub now, enhance later," no `v1`/`v2` versioning.
- **All moving deps track upstream latest.** A2UI, CopilotKit, LangGraph, Deep Agents, renderers, the `mcp` client — verify current versions before pinning. Manifests are the source of truth.
- **`alembic check`** must produce zero diff. New schema → new migration, never edit an existing one. File pattern: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.
- **`pytest -m "not integration"`** for unit; **`pytest -m integration`** for tests that need the compose stack. Mark explicitly with `@pytest.mark.integration`.
- **Pyright is strict.** `typeCheckingMode = "strict"`; 0 errors, and the warning count must not increase.
- **Ruff config** is in `pyproject.toml`; line-length 100, `target-version = py313`.
- **Naming.** Distribution: `aleph-xxx`. Python module: `aleph_xxx`. Tables: plural snake_case. Action kinds: `<entity>.<verb>`. OTEL spans: `<subsystem>.<op>`.

## When adding to the system

- **New service method** that mutates state → also writes the `ActionLedgerEvent` in the same transaction; integration test asserts the ledger row.
- **New LLM call site** → through `LiteLLMClient.chat()`/`.embed()` with a `Capability` and a `purpose`; produces a `ModelCall` + `CostLedgerEvent`.
- **New row type** → must have `project_id` + `access_scope`; no globally-scoped tables.
- **New A2UI component** → schema bump in the catalog + renderer + a producer, all in the same PR (the roster sweep enforces producer+renderer together). Right-panel components render only from bound props — no self-fetch.
- **New connector** → implement complete (`search`/`fetch`/`normalize`), register in `get_registry()`, declare `output_kind ∈ {document, dataset_rows}`. Credentials come from `ConnectorCredential` via `ConnectorCredentialService` — never from container env vars.
- **New Python package** → add to `[tool.uv.workspace] members`, `[tool.uv.sources]`, ruff/pyright `src`/`include` lists in root `pyproject.toml`; `uv sync`.
- **Migrations** → never edit an existing revision; add a new one.

## Docs map

- `docs/architecture.md` — package list + strict DAG, the load-bearing rules (incl. amended rule 8), the committed sweeps as living invariants, process boundaries.
- `docs/research-loop.md` — the native `deep_research_job` (plan→search→ingest→reflect→compose→synthesize), tool binding/allowlist, `/synthesize`, no-strand failure semantics; `aleph-scholar` (tri-state DOI verification, Consensus over MCP+OAuth, reviewer citation pass).
- `docs/workspace.md` — data-bound v0_9 surfaces + `updateDataModel` deltas + seq/resume, the reader tier + deterministic HTML compiler, the sandbox `code_runner` + versioned artifacts, agent eyes+hands, the catalog roster.
- `docs/wiki.md` — wiki-first retrieval, curator, hand-edits/aliases, freshness scoring, refresh job + ApprovalCards, retraction blast-radius, drift.
- `docs/storage.md` — the `AssetStore` protocol (fs default / s3 opt-in), key layout, the one authenticated streaming route + CSP sandbox.
- `docs/operations.md` — bootstrap, verify-gateway, the compose stack, the gate suite, migrations.
- `docs/security.md` — auth modes, agent tokens, ConnectorCredential encryption + Consensus OAuth, code_runner isolation, CSP-sandbox asset serving, ledger hash-chain, the deferred SSE×OIDC gap.
- `docs/implementation-log.md` — append-only canonical record of what shipped vs. honest gaps.
- `docs/archive/` — superseded pre-WP specs, plans, assessments, and the per-subsystem docs, preserved verbatim.
