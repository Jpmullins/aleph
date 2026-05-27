# Repo structure

Monorepo with `uv` (Python) + `pnpm` (JS) workspaces.

```
apps/
  api/                        FastAPI app — request/response + SSE later
  web/                        React + Vite SPA
  workers/                    Arq workers — long-running + agent jobs
packages/
  aleph-core/                 shared primitives (UUIDv7, errors, Pydantic schemas)
  aleph-db/                   SQLAlchemy ORM + repositories + Alembic
  aleph-security/             auth, Principal, JWT verify, agent tokens
  aleph-observability/        OTEL + Langfuse + structlog
  aleph-models/               LiteLLM gateway client + pricing + profile resolver
  aleph-evals/                eval runner skeleton (datasets land in Inc 8)
deploy/
  compose/                    docker-compose stack (Postgres+pgvector, MinIO, Redis,
                              Langfuse, OTEL collector, the three Aleph apps)
docs/
  superpowers/specs/          design specs (top-level + per-increment)
  engineering/                local dev, repo structure, quality gates, transport
  domain/                     ledger, cost, model profile
  security/                   auth, agent tokens
  operations/                 runbook
  implementation-log.md       appended after every increment
scripts/
  bootstrap-local.sh          one-command local boot
  verify-gateway.sh           gateway model-list assertion
tests/
  e2e/                        cross-package integration tests
.github/workflows/
  ci.yml                      lint + typecheck + tests + migrations + evals
  eval.yml                    nightly full eval run
```

## Package boundaries

The dependency graph is a strict DAG. Higher → lower only.

```
aleph-api ─┬─→ aleph-models ─┬─→ aleph-db ───→ aleph-core
           ├─→ aleph-security ─→ aleph-core
           ├─→ aleph-observability
           └─→ aleph-evals ─→ aleph-core

aleph-workers ─→ {aleph-models, aleph-db, aleph-security, aleph-observability, aleph-core}
```

No cycles. `aleph-core` is the leaf — never imports anything else.

## Naming conventions

- **Python packages:** `aleph_xxx` modules, `aleph-xxx` distribution names.
- **Apps:** `aleph-api`, `aleph-workers`, `aleph-web` (web is `@aleph/web` for npm).
- **Migrations:** `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.
- **Tables:** plural snake_case (`projects`, `action_ledger_events`).
- **Action kinds:** `<entity>.<verb>` (`project.create`, `model_profile.update`).
- **OTEL span names:** `<subsystem>.<op>` (`litellm.chat`, `wiki.compile`).

## Adding a new package

1. Create `packages/aleph-newpkg/pyproject.toml` declaring deps.
2. Add `aleph-newpkg` to `[tool.uv.workspace] members` and `[tool.uv.sources]` in the root `pyproject.toml`.
3. `uv sync` to register it.
4. Add a section to `docs/engineering/repo-structure.md`.
