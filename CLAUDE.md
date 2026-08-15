# CLAUDE.md

Guidance for Claude Code working in this repository.

**This file describes what is true.** Where something is planned but not built, it says so. Where
something is built but broken, it says so. The previous version of this file asserted invariants that
were false in code and CI enforcement that did not exist, and that is the single reason a broken
retrieval path survived seven work packages. Do not restore that style. If you add a claim here,
verify it first; if you cannot verify it, mark it `PLANNED` or leave it out.

---

## What Aleph is

A **general-purpose, self-improving multi-agent harness.**

The product thesis: an agent that **authors plugins for itself and activates or deactivates them as
needed**, on a kernel whose composability model makes that safe — with guardrails preventing it from
removing load-bearing capability. The kernel is the product.

The existing research capability — ingest, scholarship, a belief layer, the research loop — ships as
the **first plugin suite**, not as the thing itself.

Within that suite, the durable knowledge layer is a **web of belief**: claims are first-class,
evidence-anchored, and revised as sources are added, contradicted, or retracted. Prose (HTML
artifacts, reports) is *rendered from* that layer, never the layer itself.

## Where the project is right now

Aleph is **mid-transition on two axes at once.** Be careful: much of the code predates both.

**Axis 1 — the knowledge layer.** Built around an LLM-maintained wiki as the primary retrieval
surface; being removed. See `docs/decisions.md` D1.

- **Being removed:** wiki page compilation, the curator, the alias service, the LLM page-selector,
  FTS-over-summaries retrieval.
- **Being built:** the **Claim Spine** (`docs/belief-engine.md`) — durable claims, verbatim-anchored
  evidence, typed claim edges, derived confidence.

**Axis 2 — the harness.** Aleph is being rebuilt on an own-implemented kernel modelled on the
spatiotemporal-composability paper (revertible effects, reactive coeffects, scoped capability access).
**The kernel language and structure are an open decision** — see `docs/decisions.md` D5. Do not assume
Python for the kernel; do assume Python for the belief/scholarship plugins, which are bound to
Postgres and the transactional ledger.

**Unchanged and healthy on both axes:** ingest/RKS, `aleph-scholar`, the action ledger, model routing,
the sandboxed code runner, the asset store.

Treat wiki code as **legacy under removal**. Do not extend it, do not fix its cosmetics, do not add
tests to it. Migrate callers off it.

### A standing constraint

**Reference implementations are read, not depended on.** `deepseek-harness`, `cordis`, `prime-agent`
and `graphify` are MIT and are blueprints to reimplement and improve on. Do not add any of them — or
any `@deepseek-ai/*` package — as a runtime dependency. Ported code carries a `NOTICE`.

---

## Commands

```bash
# Setup
cp deploy/compose/.env.example deploy/compose/.env   # then set INSIGHTS_LITELLM_API_KEY
./scripts/bootstrap-local.sh                          # boot the stack
uv sync --all-packages --all-extras                   # Python deps (MUST be --all-packages)
pnpm -C apps/web install                              # JS deps

# Quality gates (these are exactly what CI runs)
uv run ruff check .
uv run ruff format --check .
uv run pyright                                        # strict, must be 0 errors
pnpm -C apps/web lint
pnpm -C apps/web build

# Tests
uv run pytest -m "not integration" -q                 # unit
uv run pytest -m integration -q                       # needs postgres+redis (see CI for env)
uv run pytest path/to/test_file.py::test_name         # single test

# Migrations (Alembic lives in apps/api)
cd apps/api && uv run alembic upgrade head
cd apps/api && uv run alembic check                   # CI asserts no model drift
cd apps/api && uv run alembic revision -m "<slug>" --autogenerate

# Local stack
docker compose -f deploy/compose/docker-compose.yml up -d
docker compose -f deploy/compose/docker-compose.yml logs -f aleph-api
./scripts/verify-gateway.sh                           # LLM gateway sanity check
```

Endpoints after bootstrap: web `:5173`, api `:8000`, copilot-runtime `:4000`, Langfuse `:3000`.
An S3-compatible object store is opt-in (`docker compose --profile s3 up -d`); the default asset
backend is the local filesystem at `data/assets`.

---

## Layout

`uv` workspace (Python 3.13, pyright strict) + `pnpm` workspace (`apps/web`, `tests/playwright`).

```
apps/
  api/              FastAPI — HTTP + SSE, owns Alembic, hosts the in-process agent
  web/              React 19 + Vite + Tailwind + @a2ui/react + CopilotKit
  workers/          arq workers — ingest, research loop, reviewers
  code-runner/      sandboxed, credential-less, network-partitioned Python executor
  copilot-runtime/  Node bridge (:4000) — one file, thin AG-UI adapter
packages/
  aleph-core          primitives, Pydantic schemas, UUIDv7. LEAF — imports nothing else.
  aleph-db            SQLAlchemy ORM + repositories + ledger
  aleph-security      auth, Principal, JWT, role gates, agent tokens
  aleph-observability OTEL + Langfuse + structlog
  aleph-models        LiteLLMClient, pricing, ModelProfile resolver
  aleph-scholar       Crossref/OpenAlex/Consensus, DOI verification. Pure HTTP, ZERO LLM calls.
  aleph-rks           Raw Knowledge Store — sources, normalization, chunks, embeddings
  aleph-belief        web of belief — patch contract, trust lattice  (NEW, incomplete)
  aleph-research      deep-research loop (plan→search→ingest→reflect→compose)
  aleph-connectors    typed connector plugins
  aleph-assistant     chat orchestration + retrieval
  aleph-reviewer      verification passes
  aleph-hypotheses    analyst hypotheses + the derived-confidence engine
  aleph-artifacts     builder agent, rendered assets, exporters
  aleph-a2ui          A2UI catalog + Python SDK glue
  aleph-notes         analyst notes
  aleph-datasets      Dataset / DatasetVersion / Observation
  aleph-evals         eval runner and scorers  (harness is NOT wired — see Known broken)
  aleph-wiki          LEGACY, under removal
```

**Strict DAG, higher → lower.** `aleph-core` is the leaf. Apps depend on packages; packages never
depend on apps; no cycles. `aleph-scholar` carries no workspace deps.

---

## Rules that are actually enforced

1. **Pyright strict, 0 errors.** CI fails otherwise.
2. **Ruff clean**, line-length 100, `target-version = py313`.
3. **`alembic check` produces no diff.** New schema → new migration; never edit an existing one.
   Pattern: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.
4. **Tests split by marker.** `@pytest.mark.integration` for anything needing postgres/redis.
5. **All LLM traffic goes through the LiteLLM gateway.** Non-agent code uses `LiteLLMClient` from
   `aleph-models`. Agent-framework code may use `langchain_openai.ChatOpenAI` **only** pointed at the
   gateway. No provider SDK is called directly.
6. **Every row carries `project_id`**, plus `created_at`, `updated_at`, `created_by`, `access_scope`.
   The only exception is `ModelProfile` templates.

## Rules that are real but only held by review

These are genuine design commitments with no automated enforcement. Do not describe them as enforced.

- **Every state mutation writes an `ActionLedgerEvent` in the same transaction.** Hash-chained,
  append-only. Verified by hand and by targeted integration tests, not by a sweep.
- **Every LLM/embed call writes a `ModelCall` + `CostLedgerEvent`.** The agent path partially bypasses
  this — see Known broken.
- **Agents never write state directly.** They call typed service methods; workers re-enter the API
  over HTTP with a minted agent token.
- **No agent-emitted code runs in the app context.** Agent code runs only in `code-runner`; its output
  is a versioned artifact rendered in a `sandbox` iframe. No agent-emitted SQL.
- **`ModelProfile` resolves capability → model.** Call sites pass a `Capability` and the project
  profile; `LiteLLMClient` resolves the binding.

---

## Known broken — do not trust these

Verified during the 2026-08 review. Fix or delete; do not build on top of.

- **Retrieval is body-blind.** `wiki_index.index_tsv` covers `title + summary + aliases` only, queried
  with `plainto_tsquery` (ANDs every term). Most natural-language questions match nothing and fall
  through to "here are some recent pages." `POST /wiki/search` has the same defect.
- **`Citation.source_page_id` is `None` on every production write path.** It has five consumers. This
  one column silently voids retraction blast-radius, two of four freshness dimensions, the reviewer's
  source registry, and the citation popover. Integration tests pass because fixtures hand-build the row.
- **Stale-link expansion.** `wiki_service.py` deletes `WikiLink` rows for the *new* revision id (always
  a no-op), and `retrieval/router.py` expands links with no `src_revision_id` filter — so retrieval
  traverses every historical revision's links forever.
- **`commit_revision` is not atomic on the path agents use.** It only row-locks when given a
  `page_id`; the by-title path returns the page unlocked and computes `revision_no` as `max+1`.
- **The eval harness never invokes the system.** Scorers read `expected` and `actual` from the same
  fixture dict. The datasets and the CI gate have been deleted; the scorers remain and need a real
  harness before they mean anything.
- **Freshness is the constant 50** on every page, and the Wiki tab sorts by it.
- **The agent endpoint bypasses auth middleware.** It is on the skip list, performs no verification of
  its own, and derives project scope from client-supplied state. Every other route is correctly gated.
- **Cost attribution has a hole.** The `ChatOpenAI` agent path does not always produce `ModelCall` rows.

---

## When adding to the system

- **New service method that mutates state** → write the `ActionLedgerEvent` in the same transaction,
  and add an integration test asserting the ledger row.
- **New LLM call site** → `LiteLLMClient.chat()`/`.embed()` with a `Capability` and a `purpose`.
- **New row type** → `project_id` + `access_scope`. No globally-scoped tables.
- **New connector** → implement `search`/`fetch`/`normalize`, register in `get_registry()`, declare
  `output_kind`. Credentials come from `ConnectorCredential`, never from container env.
- **New Python package** → add to `[tool.uv.workspace] members`, `[tool.uv.sources]`, and both the
  ruff `src` and pyright `include` lists in the root `pyproject.toml`; then `uv sync`.
- **Ported code** → add a `NOTICE` recording upstream, license, and per-file lineage. See
  `packages/aleph-belief/NOTICE`.

**Ship a consumer with every producer.** The dominant defect class in this codebase is a column,
table, or service that is written correctly and read by nothing. A contract with no caller is not
progress. If you add a write path, add the read path in the same change or do not add it.

## Naming

Distribution `aleph-xxx` · module `aleph_xxx` · tables plural snake_case · action kinds
`<entity>.<verb>` · OTEL spans `<subsystem>.<op>`.

## Docs

- `docs/architecture.md` — what exists today, honestly
- `docs/belief-engine.md` — the Claim Spine design being built
- `docs/decisions.md` — why the wiki is being removed, and what was borrowed from where
- `docs/operations.md` — stack, migrations, gates
