# Operations

## First-time setup

```bash
cp deploy/compose/.env.example deploy/compose/.env   # then edit secrets
./scripts/bootstrap-local.sh                          # boot the full stack
```

Set at minimum `INSIGHTS_LITELLM_API_KEY` and the `LANGFUSE_*` random values (`openssl rand -hex 32`). There is no external model/GPU registry login step — the research loop is native and in-process.

## `bootstrap-local.sh`

A thin convenience wrapper (the stack is self-bootstrapping via compose one-shots):

1. Asserts `deploy/compose/.env` exists.
2. `mkdir -p data/assets` and exports `ALEPH_UID`/`ALEPH_GID` (`id -u`/`id -g`) so the bind-mounted asset dir stays owned by the invoking user — `aleph-api`/`aleph-workers` run as that uid and must be able to write assets.
3. `docker compose up -d --build`, then waits for `http://localhost:8000/healthz`.
4. Runs `scripts/verify-gateway.sh`.

It boots **no object-store container** (fs is the default asset backend) and starts the isolated `aleph-code-runner` + `code-runner-redis` on the Redis-only internal network.

## `verify-gateway.sh`

Sources `.env`, requires `INSIGHTS_LITELLM_API_KEY`, and `GET`s `${LITELLM_BASE_URL}/v1/models` (default `https://gateway.insights.arlis.umd.edu`), asserting the required model ids are present (Opus/Sonnet/Haiku + the Cohere embed models). Fails loudly if any are missing.

## The compose stack

`deploy/compose/docker-compose.yml`. All services bind to `127.0.0.1`; each carries `mem_limit` = `memswap_limit` (no swap; OOM-kill inside the cgroup rather than paging the host). Long-running caps total ~9.5g.

Default services: `postgres` (pgvector 0.8.2 / pg18), `redis` (platform bus), `code-runner-redis` (dedicated code-job bus, internal only), `aleph-code-runner` (isolated sandbox), the Langfuse v3 constellation (`langfuse`, `langfuse-worker`, `clickhouse`, `langfuse-redis`, plus Langfuse's own bundled S3-compatible object store + its init one-shot — internal to Langfuse, entirely separate from the platform asset backend), `otel-collector`, `aleph-migrate` (one-shot Alembic upgrade), `aleph-api`, `aleph-workers`, `aleph-web`, `aleph-copilot-runtime`.

- **fs is the default asset backend.** The object-store services are under `profiles: ["s3"]` — start them only with `docker compose --profile s3 up -d` when testing the s3 backend.
- **There is no separate research-subsystem service** and no external GPU-registry image.
- `aleph-migrate` runs `alembic upgrade head` on the main DB and exits; `aleph-api`/`aleph-workers` gate on it via `service_completed_successfully` so they never serve an unmigrated schema. `postgres-initdb.sh` creates the aux `langfuse` DB on a fresh volume only.
- Networks: `default` (bridge) + `code-runner-net` (`internal: true`). `aleph-workers` dual-homes onto both to dispatch code jobs; the sandbox reaches only `code-runner-redis`.

### Endpoints after bootstrap

- web `:5173`, api `:8000`, copilot-runtime `:4000`, Langfuse `:3000`.
- Object-store console `:9001` — **only** under the `s3` profile.

## Common commands

```bash
# Install deps
uv sync --all-packages --all-extras   # Python — MUST be --all-packages
pnpm -C apps/web install               # JS (the pnpm workspace is apps/web + tests/playwright)

# Lint / format / typecheck
uv run ruff check .
uv run ruff format --check .
uv run pyright
pnpm -C apps/web typecheck
pnpm -C apps/web lint

# Tests
uv run pytest -m "not integration" -q          # unit
uv run pytest -m integration -q                # integration (needs compose stack + migrations)
uv run pytest path/to/test_file.py::test_name  # single test

# Evals (CI gate)
uv run python -m aleph_evals --datasets all --gate strict

# Web build / dev
pnpm -C apps/web dev                            # vite dev server on :5173
pnpm -C apps/web build                          # tsc --noEmit && vite build

# Compose
docker compose -f deploy/compose/docker-compose.yml up -d
docker compose -f deploy/compose/docker-compose.yml logs -f aleph-api
docker compose -f deploy/compose/docker-compose.yml down
```

## Migrations

Alembic lives under `apps/api/alembic`. Models register through `env.py`.

```bash
cd apps/api && uv run alembic upgrade head
cd apps/api && uv run alembic check                        # CI asserts no model drift
cd apps/api && uv run alembic revision -m "<slug>" --autogenerate
```

Never edit an existing revision — always add a new one. File pattern: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.

## The gate suite (CI: `.github/workflows/ci.yml`)

`lint-and-typecheck` runs the web typecheck/lint, the three committed sweeps (`check-no-self-fetch.sh`, `check-catalog-roster.sh`, `check-route-reachability.sh`) plus the WP-7 doc guards (`check-docs-drift.sh`, `check-claude-commands.sh`), then ruff check/format and pyright (strict; 0 errors, warning count must not increase). `unit-tests` runs `pytest -m "not integration"`. `integration-tests` boots Postgres + Redis, runs `alembic upgrade head` + `alembic check`, then `pytest -m integration` (asset backend `fs`, rooted in the runner workspace — no object-store container). `evals` runs the strict gate. `build-web` builds the SPA.
