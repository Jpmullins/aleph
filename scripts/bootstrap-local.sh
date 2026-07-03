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

# The asset bind mount must exist BEFORE compose up, owned by the invoking
# user — otherwise the Docker daemon creates it root-owned and the api/workers
# containers (which run as ALEPH_UID:ALEPH_GID) can't write a single asset.
mkdir -p "$ROOT/data/assets"
export ALEPH_UID="$(id -u)" ALEPH_GID="$(id -g)"

# The stack is now self-bootstrapping — `docker compose up` is sufficient on a
# fresh volume. This script is a thin convenience wrapper that adds --build, a
# readiness wait, and the gateway check. The bootstrap steps live in compose:
#   - aux DBs (langfuse, aiq_*) + AIQ schema  → postgres first-init hook
#     (deploy/compose/postgres-initdb.sh, runs only on an empty volume)
#   - Alembic migrations on the main DB        → aleph-migrate one-shot service
# aleph-api / aleph-workers gate on the one-shots via service_completed_successfully.
# Asset bytes live on the local fs (data/assets bind mount) — no MinIO in the
# default stack; `--profile s3` opts back in for s3-backend testing.

echo "→ Building + starting the Aleph stack (docker compose up -d --build)"
echo "   First run pulls nvcr.io/nvidia/blueprint/aiq-agent:2.1.0. If it fails:"
echo "   docker login nvcr.io  (user: \$oauthtoken, pass: \$NGC_API_KEY)"
"${DC[@]}" up -d --build

echo "→ Waiting for aleph-api (migrations apply before it starts)"
until curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; do
  sleep 1
done

echo "→ Verifying LiteLLM gateway"
"$ROOT/scripts/verify-gateway.sh"

echo "✓ Aleph stack up:"
echo "    web      http://localhost:5173"
echo "    api      http://localhost:8000"
echo "    aiq      http://localhost:8001"
echo "    langfuse http://localhost:3000"
