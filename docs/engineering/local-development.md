# Local Development

## Prerequisites

- Docker + Docker Compose (Docker Desktop on macOS/Windows; native engine on Linux)
- `uv` (Astral) for Python — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `pnpm` for JS — `corepack enable pnpm`
- Postgres client (`psql`) — optional, for inspecting the DB

## First-time setup

```bash
cp deploy/compose/.env.example deploy/compose/.env
# Edit deploy/compose/.env:
#   - INSIGHTS_LITELLM_API_KEY=<your gateway bearer>
#   - LANGFUSE_NEXTAUTH_SECRET / LANGFUSE_SALT / LANGFUSE_ENCRYPTION_KEY
#     openssl rand -hex 32 → paste 3 different values into these
#   - ALEPH_AGENT_TOKEN_SECRET=$(openssl rand -hex 32)

./scripts/bootstrap-local.sh
```

After bootstrap the following are up:

| Service | URL | Notes |
|---|---|---|
| Web | http://localhost:5173 | Vite dev server |
| API | http://localhost:8000 | FastAPI |
| Langfuse | http://localhost:3000 | trace + eval UI |
| MinIO console | http://localhost:9001 | login: `aleph` / `changeme-local` |

## Day-to-day

```bash
# stop everything
docker compose -f deploy/compose/docker-compose.yml down

# start everything (after bootstrap)
docker compose -f deploy/compose/docker-compose.yml up -d

# tail API logs
docker compose -f deploy/compose/docker-compose.yml logs -f aleph-api

# run a one-off Alembic migration locally (without the API container)
cd apps/api && DATABASE_URL=postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph \
  uv run alembic upgrade head
```

## Running tests

```bash
# unit only
uv run pytest -m "not integration" -q

# integration (needs compose stack + migrations applied)
uv run pytest -m integration -q

# typecheck
uv run pyright

# lint + format check
uv run ruff check .
uv run ruff format --check .

# web
pnpm -C apps/web typecheck
pnpm -C apps/web lint
pnpm -C apps/web build
```

## Auth in dev

The default `.env.example` points to a Keycloak realm at `localhost:8080`.
For local UI work without Keycloak, use the API directly with a fake
`Authorization: Bearer` header and patch in the test-only auth bypass.
Production deployments use a real OIDC IdP (Auth0, Cognito, Keycloak).

## Common issues

| Symptom | Fix |
|---|---|
| `pg_isready` keeps spinning on bootstrap | Disk I/O slow; wait. Check `docker compose logs postgres`. |
| `/readyz` reports `litellm_gateway: { ok: false }` | Set `INSIGHTS_LITELLM_API_KEY` in `.env`. |
| Langfuse refuses to start | The `langfuse` DB must exist on the shared Postgres. `bootstrap-local.sh` creates it; on first failure re-run the script. |
| Alembic `relation does not exist` after model changes | Add a new revision; never edit Inc 0's migration. |
