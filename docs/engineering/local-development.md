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
#   - INSIGHTS_LITELLM_API_KEY=<gateway bearer>
#   - NGC_API_KEY=<NGC personal API key, for pulling the aiq-server image>
#   - LANGFUSE_NEXTAUTH_SECRET / LANGFUSE_SALT / LANGFUSE_ENCRYPTION_KEY
#     openssl rand -hex 32 → three different values
#   - LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY → any values; then
#     LANGFUSE_AUTH=$(echo -n "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" | base64 -w0)
#   - ALEPH_AGENT_TOKEN_SECRET=$(openssl rand -hex 32)
#   - POSTGRES_PASSWORD / MINIO_ROOT_PASSWORD → any strong values
# Leave ALEPH_AUTH_MODE=local for now.

# One-time NGC login so docker can pull nvcr.io/nvidia/blueprint/aiq-agent:
echo "$NGC_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin

./scripts/bootstrap-local.sh
```

After bootstrap the following are up:

| Service | URL | Notes |
|---|---|---|
| Web | http://localhost:5173 | Vite dev server |
| API | http://localhost:8000 | FastAPI |
| AIQ | http://localhost:8001 | NVIDIA AI-Q research subsystem |
| Langfuse | http://localhost:3000 | trace + eval UI |
| MinIO console | http://localhost:9001 | login: see `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` in `.env` |

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

Local dev runs with `ALEPH_AUTH_MODE=local`: the API skips OIDC verification entirely and JIT-provisions a fixed `dev@aleph.local` user on first request. No IdP service runs locally — there is no Keycloak in the compose stack. The frontend mirrors this with `VITE_AUTH_MODE=local`; it skips `oidc-client-ts` and sends a sentinel bearer the API recognizes.

For production, set `ALEPH_AUTH_MODE=oidc` and fill in `ALEPH_AUTH_ISSUER`, `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL` against any OIDC IdP (Cognito, Auth0, Authentik, Keycloak, ALB OIDC). See `docs/security/auth.md` for the per-IdP env shape.

## Common issues

| Symptom | Fix |
|---|---|
| `pg_isready` keeps spinning on bootstrap | Disk I/O slow; wait. Check `docker compose logs postgres`. |
| `/readyz` reports `litellm_gateway: { ok: false }` | Set `INSIGHTS_LITELLM_API_KEY` in `.env`. |
| Langfuse refuses to start | The `langfuse` DB must exist on the shared Postgres. `bootstrap-local.sh` creates it; on first failure re-run the script. |
| Alembic `relation does not exist` after model changes | Add a new revision; never edit Inc 0's migration. |
