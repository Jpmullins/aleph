# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Aleph is a multi-agent research environment built around three layers:
1. **RKS** (Raw Knowledge Store) — ingested sources, normalized text, chunks. Upstream of the wiki.
2. **Compiled Wiki** — the **primary retrieval surface** for both assistant and analyst. Wikilinked, revisioned, multi-agent-maintained.
3. **A2UI workspace** — 3-panel UI; right panel is 5 A2UI surface tabs (Wiki / Library / Notes / Hypotheses / Briefs). The **Library** tab (formerly "Artifacts") shows ingested **Sources** — raw PDFs/webpages/docs, viewable in their own card — alongside built **Artifacts**.

Authoritative spec: `docs/superpowers/specs/2026-05-26-aleph-design.md`. Per-increment specs: `docs/superpowers/specs/2026-05-27-inc-{0..8}-*-design.md`. Build is **complete through Increment 8**, plus post-Inc-8 **Waves** (all merged to `main`): W1 (progress + tokens), W2 (CopilotKit + AG-UI + A2UI Live agent), W5 (ACH matrix, notes promote-to-wiki, readable sources), the AIQ research→wiki pipeline + conversational research, **W6** (conversational completion — Live is the only chat surface; agent tool suite; ApprovalCard-gated actions; agent cost attribution; per-project cross-session memory; cost UI in Profile), **W4** (A2UI **v0_9** shared catalog — right panel + chat render via one upstream `@a2ui` catalog; backend emits v0_9 messages + SSE delta `SurfaceStreamer`), and **W3** (the Live assistant is now a Deep-Agents **orchestrator** that plans via `write_todos` and delegates to 6 purpose-built subagents — retriever/researcher/wiki_builder/viz_builder/analyst/reviewer — each wrapping existing services in isolated context; + SKILL.md skills + todos-in-Activity-card), and the **Real-time push + Live Wiki** wave (every SSE stream — agent-events, surfaces, assistant, and a new `changes` stream — now wakes on a Postgres **LISTEN/NOTIFY** push with a self-healing poll fallback instead of idle polling; the wiki tab updates the instant an agent writes, with "✦ editing…" presence + an "updated" pulse and the open page refreshing in place; see `docs/superpowers/specs/2026-05-29-live-wiki-design.md`). Refreshed designs: `docs/superpowers/specs/2026-05-29-wave-{3,4}-*-refresh-design.md` + `2026-05-29-wave-6-conversational-completion-design.md` (the original `wave-{3,4}-*-design.md` are **superseded**). The canonical record (what shipped vs. honest gaps) is `docs/implementation-log.md`; the latest system review + prioritized gaps is `docs/system-assessment.md`.

## Common commands

```bash
# First-time setup
cp deploy/compose/.env.example deploy/compose/.env   # then edit secrets
echo "$NGC_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin  # one-time, for aiq-server pull
./scripts/bootstrap-local.sh                          # boot full stack

# Install deps
uv sync --all-packages --all-extras                   # Python (MUST be --all-packages: installs every workspace member; --all-extras alone leaves them uninstalled → import/pyright failures)
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

Endpoints after bootstrap: web `:5173`, api `:8000`, copilot-runtime `:4000`, aiq-server `:8001`, Langfuse `:3000`, MinIO console `:9001`.

## Architecture

Monorepo: `uv` workspace (Python 3.13, pyright strict) + `pnpm` workspace (only contains `apps/web`).

```
apps/
  api/      FastAPI — request/response + SSE; hosts WikiIndex page-selector; owns Alembic
  web/      React 19 + Vite + Tailwind + @a2ui/react + CopilotKit
  workers/  Arq workers — wiki agent, reviewers, builder, normalization, intra-source chunk+embed, curator, re-embed (NOTE: a Playwright JS-page **render worker** is specced but NOT yet built — URL ingest is raw-HTTP, so JS/SPA pages capture as static HTML)
packages/
  aleph-core              shared primitives, Pydantic schemas, UUIDv7. LEAF — imports nothing else.
  aleph-db                SQLAlchemy ORM + repositories + Alembic models
  aleph-security          auth, Principal, JWT, role gates, agent tokens
  aleph-observability     OTEL + Langfuse + structlog
  aleph-models            LiteLLMClient, pricing, ModelProfile resolver
  aleph-rks               Raw Knowledge Store domain
  aleph-wiki              wiki compile / index / aliases / hand-edits
  aleph-assistant         chat orchestration, wiki retrieval router
  aleph-aiq               AIQ subsystem client (HTTP boundary)
  aleph-a2ui              A2UI catalog + Python SDK glue
  aleph-connectors        typed connector plugins (document / dataset_rows) — NOTE: the `ConnectorBase`/`ConnectorRegistry` suite is currently **orphaned** (its `search`/`fetch` are not on the live research path). Research runs against AIQ's own built-in `data_source_registry` tools (effectively Tavily web search). Wiring the typed connectors in requires a custom AIQ image with NAT plugins (sequenced infra)
  aleph-reviewer          MechanicalReviewer + EditorialReviewer
  aleph-hypotheses        analyst-authored hypotheses
  aleph-datasets          Dataset / DatasetVersion / Observation
  aleph-artifacts         Builder agent, RenderedAssets, exporters
  aleph-notes             analyst notebook
  aleph-evals             eval runner, datasets, scorers, CI gates
```

**Strict DAG, higher → lower only.** `aleph-core` is the leaf. `aleph-api` and `aleph-workers` depend on packages; packages never depend on apps; no cycles.

### The load-bearing rules

These are enforced by code review and the eval gates — not aspirational.

1. **Wiki-first retrieval.** Primary path is `WikiIndex page-selector LLM → load pages + 1-hop wikilinks → answer composer`. Embeddings (`DocumentChunk` over pgvector) are **only** used for intra-source descent (step 4 of the retrieval flow), never as first-line RAG. Don't add a "secret RAG" shortcut.
2. **All LLM traffic routes through the Insights LiteLLM gateway.** Non-agent code (wiki compile, reviewers, embeddings, page-selection, etc.) imports `LiteLLMClient` from `aleph-models`. Agent-framework code (CopilotKit / LangGraph / Deep Agents nodes) MAY use `langchain_openai.ChatOpenAI` **only** when pointed at the gateway (`base_url=LITELLM_BASE_URL`, `api_key=INSIGHTS_LITELLM_API_KEY`) — see Wave 2. **No provider SDK is called directly** (`anthropic`, `google-genai`, or `openai`/`ChatOpenAI` against a real provider base_url). The historical gap (ChatOpenAI bypassing `LiteLLMClient` and not writing `ModelCall`/`CostLedgerEvent`) is **closed** as of W6: `apps/api/src/aleph_api/copilot_cost_callback.py` (`AgentCostCallbackHandler`) is attached to the orchestrator model and to every subagent's model via `subagent_model(name)`, writing `ModelCall`+`CostLedgerEvent` per call (`purpose="assistant.turn"` / `assistant.subagent.<name>`) without double-counting the `LiteLLMClient` path. Requires `stream_usage=True` on `ChatOpenAI` (streaming otherwise drops usage).
3. **Agent → service is the only path to state.** Agents (Aleph or AIQ) never write to Postgres or S3 directly. They call typed `aleph-api` service methods. AIQ connectors call back into `aleph-api` over an internal RPC for credentials, scoping, and persistence.
4. **Every mutation writes an Action Ledger event in the same transaction.** Hash-chained, append-only, no deletes. Integration tests assert ledger-event-count per mutation.
5. **Every LLM/tool/embed call writes a `ModelCall` + `CostLedgerEvent`** and is wrapped in an OTEL/Langfuse span. Cost is computed in `aleph_models.pricing` (cache-discount-aware).
6. **Every row carries `project_id`** + `created_at`, `updated_at`, `created_by`, `access_scope`, optional `trace_id`, `ledger_event_id`. No global resources (only exception: `ModelProfile` templates).
7. **`ModelProfile` resolves capability → model.** Call sites pass a `Capability` (`synthesis`, `extraction`, `page_selection`, `classification`, `embedding`, `rerank`, `vision`, `code`, `judge`) and the project's profile; `LiteLLMClient` resolves the binding. Two named profiles: `aleph-dev` (Sonnet/Haiku) and `aleph-production` (Opus/Sonnet). Selected by `ALEPH_DEFAULT_MODEL_PROFILE`.
8. **A2UI surfaces are declarative.** Agents request components by name + props; the React renderer validates against JSON Schema. No agent-emitted JavaScript, no agent-emitted SQL.

### Process boundaries

- `aleph-api` — synchronous HTTP + SSE, holds user-identity boundary, owns Alembic, hosts the page-selector.
- `aleph-workers` — long-running agent jobs (LangGraph / Deep Agents), reviewers, normalization, render. Holds short-lived agent tokens, not raw DB credentials.
- `aiq-server` — separate compose service running NVIDIA AIQ as the **research** subsystem. Pulled from `nvcr.io/nvidia/blueprint/aiq-agent:2.1.0` (requires NGC login). Boot config at `deploy/compose/aiq-config-default.yml` routes all LLM traffic through the Insights LiteLLM gateway (`_type: openai`) and wires a `data_source_registry` web-search tool (Tavily) so research captures sources. AIQ does not write directly to Postgres or S3; its tool calls re-enter through `aleph-api`. Its real HTTP API: health `GET /health`, submit `POST /v1/jobs/async/submit {agent_type, input}` (`deep_researcher`|`shallow_researcher`), results at `/v1/jobs/async/job/{id}` + `/report`. AIQ's job-store schema (`job_info`, …) is **not** auto-created — `bootstrap-local.sh` applies `deploy/compose/aiq-init-{jobs,checkpoints}.sql`. AIQ output is consumed by the `aiq_synthesis_poll_job` worker → `synthesis_workflow` → a **synthesis proposal** in Briefs, never published directly.
- `aleph-copilot-runtime` — Node `@copilotkit/runtime` v2 service (port :4000) bridging the React app to `aleph-api`'s AG-UI Deep Agent endpoint; where A2UI tool injection + the inline "aleph" catalog live (Wave 2). Dockerfile uses `npm` (pnpm 10 blocks esbuild/@scarf build scripts).

### Auth modes

`ALEPH_AUTH_MODE` selects the user-auth path:
- `local` (compose default) — JWT verification skipped; every non-public request maps to a fixed `dev@aleph.local` principal, JIT-provisioned on first sight. Agent tokens (HS256, internal) still verified. No IdP service runs locally.
- `oidc` — full JWT/JWKS verification against any OIDC IdP (Cognito, Auth0, Authentik, Keycloak, ALB OIDC). Three env vars (`ALEPH_AUTH_ISSUER`, `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL`).

The OIDC code path is dormant in local mode but kept intact so deploy is a config flip, not a rewrite. Frontend mirrors via `VITE_AUTH_MODE`.

## Engineering rules

Hard, enforced by CI:

- **No placeholder code in production paths.** CI greps for `TODO|FIXME|NotImplementedError` outside `tests/`.
- **Each increment ships its scope in final production form.** No "stub now, enhance later," no `v1`/`v2` versioning. Out-of-scope items are explicit in §16.1 of the design spec; sequenced items are listed in §16.2.
- **All moving deps track upstream latest.** A2UI, AIQ, CopilotKit, LangGraph, Deep Agents, NAT, renderers — verify actual current versions via npm/PyPI/GitHub before pinning. Manifests (`package.json`, `pyproject.toml`) are the source of truth; specs name packages, not versions.
- **`alembic check`** must produce zero diff. New schema → new migration, never edit Inc 0's. File pattern: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.
- **`pytest -m "not integration"`** for unit; **`pytest -m integration`** for tests that need the compose stack. Mark explicitly with `@pytest.mark.integration`.
- **Pyright is strict.** `typeCheckingMode = "strict"`. Treat `reportUnknown*` warnings as work to do, not noise.
- **Ruff config** is in `pyproject.toml`; line-length 100, `target-version = py313`. Per-file ignores already cover tests (`S101`) and alembic versions (`E501`).
- **Naming.** Distribution: `aleph-xxx`. Python module: `aleph_xxx`. Tables: plural snake_case. Action kinds: `<entity>.<verb>`. OTEL spans: `<subsystem>.<op>` (e.g. `litellm.chat`, `wiki.compile`).

## When adding to the system

- **New service method** that mutates state → also writes the `ActionLedgerEvent`, in the same transaction; integration test asserts the ledger row.
- **New LLM call site** → goes through `LiteLLMClient.chat()` / `.embed()` with a `Capability` and a `purpose` string; produces a `ModelCall` + `CostLedgerEvent`. The `purpose` is what shows up in Langfuse.
- **New row type** → must have `project_id` + access_scope; no globally-scoped tables.
- **New A2UI component** → schema bump in the catalog + renderer ship in the same PR. Don't ship one without the other.
- **New connector** → implement complete (`search` / `fetch` / `normalize`), register as a `nat` function, declare `output_kind ∈ {document, dataset_rows}`. Credentials come from `ConnectorCredential` via callback into `aleph-api` — never from container env vars.
- **New Python package** → add to `[tool.uv.workspace] members`, `[tool.uv.sources]`, ruff/pyright `include` lists in root `pyproject.toml`; `uv sync`.
- **Migrations** → never edit an existing revision; add a new one.

## Notable references in `~/code`

External reference clones the design depends on:
- `~/code/A2UI` — A2UI core + React renderer + Python agent SDK (we use these from npm/PyPI, but the clone is the reference for tracked-upstream-latest decisions).
- `~/code/aiq` — NVIDIA AI-Q Blueprint v2.1.0 source.
- `~/code/obsidian-llm-wiki-local` — practical Karpathy-LLM-Wiki pattern; we adopted its retrieval, hand-edit, rejection-feedback, alias, and source-page patterns.

## Docs map

**Specs & plans**
- `docs/superpowers/specs/` — design specs (top-level `2026-05-26-aleph-design.md` + per-increment `2026-05-27-inc-{0..8}-*`)
- `docs/superpowers/specs/2026-05-29-wave-{3,4}-*-refresh-design.md` + `2026-05-29-wave-6-conversational-completion-design.md` + `2026-05-29-live-wiki-design.md` — the **shipped**-wave designs (W3 orchestrator+subagents; W4 A2UI v0_9; W6 conversational completion; realtime-push + live wiki), each with a References section pinning the exact local repos / MCP servers / skills. The original `wave-{3,4}-*-design.md` are superseded (banners at their top).
- `docs/superpowers/plans/` — the implementation plans for waves 3 / 4 / 6

**Canonical status**
- `docs/implementation-log.md` — appended after every increment/wave (the canonical record of what shipped vs. honest gaps); entries for W6, W4, W3, the realtime-push + live-wiki wave + the 2026-05-28/29 session entry with the full reference map (local repos, a2ui.org, MCP servers, skills)
- `docs/system-assessment.md` — the 2026-05-29 full system review (updated 2026-05-30): what works (verified), quality-gate reality, and the prioritized gap list

**Engineering & ops**
- `docs/engineering/` — `repo-structure.md`, `local-development.md`, `quality-gates.md`, `litellm-transport.md`, `tests-per-increment.md`
- `docs/operations/` — `runbook.md`, `aiq-runbook.md`
- `docs/security/` — `auth.md` (incl. the deferred SSE×OIDC gap), `aiq-boundary.md`, `connector-credentials.md`

**Subsystem docs**
- `docs/domain/` — `wiki.md`, `rks.md`, `claims-and-provenance.md`, `ledger.md`
- `docs/agents/` — `wiki-agent.md`, `assistant-agent.md`, `research-agent.md`, `synthesize-action.md`, `aiq-config.md`
- `docs/a2ui/` — `surfaces.md`, `action-router.md`, `catalog.md`; `docs/ui/` — `chat-surface.md`
- `docs/retrieval/` — `wiki-first-router.md`; `docs/pipelines/` — `chunking-and-embedding.md`, `normalization.md`
- `docs/wiki/` — `aliases.md`, `hand-edits.md`, `rejection-feedback.md`
- `docs/connectors/` — `connector-contract.md`, `upload.md`
- `docs/reviewers/` — `mechanical.md`, `editorial.md`, `approval-workflow.md`
- `docs/hypotheses/` — `hypotheses.md`; `docs/datasets/` — `datasets.md`
- `docs/artifacts/` — `builder.md`, `exporters.md`; `docs/evals/` — `eval-suite.md`, `regression-suite.md`
