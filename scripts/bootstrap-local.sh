#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/deploy/compose/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "✗ Missing $ENV_FILE."
  echo "  Run: cp deploy/compose/.env.example deploy/compose/.env"
  echo "  Then edit it and set INSIGHTS_LITELLM_API_KEY plus the LANGFUSE_* random values."
  exit 1
fi

DC=(docker compose -f deploy/compose/docker-compose.yml --env-file "$ENV_FILE")

# Load .env so we can read POSTGRES_USER/etc. on the host. The compose
# stack reads it via --env-file; we need it here for the host-side
# alembic run and the psql commands below.
set -o allexport
# shellcheck source=/dev/null
. "$ENV_FILE"
set +o allexport

PG_USER="${POSTGRES_USER:-aleph}"
PG_PASS="${POSTGRES_PASSWORD:-changeme-local}"
PG_DB="${POSTGRES_DB:-aleph}"
DATABASE_URL_HOST="postgresql+asyncpg://${PG_USER}:${PG_PASS}@localhost:5432/${PG_DB}"

echo "→ Bringing up infra services (postgres, minio, redis, langfuse, otel-collector)"
"${DC[@]}" up -d postgres minio redis langfuse otel-collector

echo "→ Waiting for Postgres"
until "${DC[@]}" exec -T postgres pg_isready -U "${PG_USER}" >/dev/null 2>&1; do
  sleep 1
done

# Create the auxiliary DBs (langfuse + the three AIQ ones) on the
# shared Postgres instance. Idempotent.
ensure_db() {
  local name=$1
  "${DC[@]}" exec -T postgres psql -U "${PG_USER}" -d postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname='${name}'" | grep -q 1 \
    || "${DC[@]}" exec -T postgres psql -U "${PG_USER}" -d postgres -c \
         "CREATE DATABASE ${name}"
}
echo "→ Ensuring auxiliary DBs (langfuse, aiq_jobs, aiq_checkpoints, aiq_summary)"
ensure_db langfuse
ensure_db aiq_jobs
ensure_db aiq_checkpoints
ensure_db aiq_summary

# AIQ's job-store + checkpoint tables are NOT auto-created by the
# aiq-agent image (only job_events is). Without job_info, every research
# job submission 500s. Apply the vendored schema idempotently.
echo "→ Applying AIQ job-store + checkpoint schema"
"${DC[@]}" exec -T postgres psql -U "${PG_USER}" -d aiq_jobs -v ON_ERROR_STOP=1 \
  < "$ROOT/deploy/compose/aiq-init-jobs.sql"
"${DC[@]}" exec -T postgres psql -U "${PG_USER}" -d aiq_checkpoints -v ON_ERROR_STOP=1 \
  < "$ROOT/deploy/compose/aiq-init-checkpoints.sql"

echo "→ Running Alembic migrations"
(cd apps/api && DATABASE_URL="${DATABASE_URL_HOST}" uv run alembic upgrade head)

echo "→ Initializing MinIO bucket"
"${DC[@]}" exec -T minio sh -c \
  "mc alias set local http://localhost:9000 \"${MINIO_ROOT_USER:-aleph}\" \"${MINIO_ROOT_PASSWORD:-changeme-local}\" >/dev/null && \
   mc mb --ignore-existing local/${ALEPH_S3_BUCKET:-aleph-local} >/dev/null"

echo "→ Verifying LiteLLM gateway"
"$ROOT/scripts/verify-gateway.sh"

echo "→ Starting aiq-server (pulling nvcr.io/nvidia/blueprint/aiq-agent:2.0.0)"
echo "   If pull fails: docker login nvcr.io  (user: \$oauthtoken, pass: \$NGC_API_KEY)"
"${DC[@]}" up -d aiq-server

echo "→ Building + starting aleph-api, aleph-workers, aleph-web"
"${DC[@]}" up -d aleph-api aleph-workers aleph-web

echo "✓ Aleph stack up:"
echo "    web      http://localhost:5173"
echo "    api      http://localhost:8000"
echo "    aiq      http://localhost:8001"
echo "    langfuse http://localhost:3000"
echo "    minio    http://localhost:9001"
