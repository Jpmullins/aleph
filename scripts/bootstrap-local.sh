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

echo "→ Bringing up infra services (postgres, minio, redis, langfuse, otel-collector)"
"${DC[@]}" up -d postgres minio redis langfuse otel-collector

echo "→ Waiting for Postgres"
until "${DC[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER:-aleph}" >/dev/null 2>&1; do
  sleep 1
done

# Ensure langfuse's own DB exists (single-pg deployment).
echo "→ Ensuring 'langfuse' DB exists"
"${DC[@]}" exec -T postgres psql -U "${POSTGRES_USER:-aleph}" -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='langfuse'" | grep -q 1 \
  || "${DC[@]}" exec -T postgres psql -U "${POSTGRES_USER:-aleph}" -d postgres -c \
       "CREATE DATABASE langfuse"

echo "→ Running Alembic migrations"
(cd apps/api && DATABASE_URL="${DATABASE_URL_HOST:-postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph}" uv run alembic upgrade head)

echo "→ Initializing MinIO bucket"
"${DC[@]}" exec -T minio sh -c \
  "mc alias set local http://localhost:9000 \"${MINIO_ROOT_USER:-aleph}\" \"${MINIO_ROOT_PASSWORD:-changeme-local}\" >/dev/null && \
   mc mb --ignore-existing local/${ALEPH_S3_BUCKET:-aleph-local} >/dev/null"

echo "→ Verifying LiteLLM gateway"
"$ROOT/scripts/verify-gateway.sh"

echo "→ Building + starting aleph-api, aleph-workers, aleph-web"
"${DC[@]}" up -d aleph-api aleph-workers aleph-web

echo "✓ Aleph stack up:"
echo "    web      http://localhost:5173"
echo "    api      http://localhost:8000"
echo "    langfuse http://localhost:3000"
echo "    minio    http://localhost:9001"
