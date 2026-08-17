#!/usr/bin/env bash
#
# Is the configured LLM gateway reachable, and does it serve what we need?
#
# Reachability only. The authoritative check — "every model bound in every
# ModelProfile is served, and the embedder's width matches the column" — needs
# the database, and lives in `scripts/_acceptance/gateway_serves_bound_models.py`
# (acceptance H1). This script is the fast one you run before booting.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/compose/.env"

# Fill only what the caller has not already set. `set -a; . .env` would clobber
# an explicit `LITELLM_BASE_URL=... ./scripts/verify-gateway.sh`, which is the
# normal way to point this at a gateway other than the configured one.
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    case "$key" in *[!A-Za-z0-9_]*|'') continue ;; esac
    [ -n "${!key:-}" ] || export "$key=${line#*=}"
  done < "$ENV_FILE"
fi

if [ -z "${INSIGHTS_LITELLM_API_KEY:-}" ]; then
  echo "✗ INSIGHTS_LITELLM_API_KEY not set; can't verify gateway."
  exit 1
fi

BASE_URL="${LITELLM_BASE_URL:-https://gateway.insights.arlis.umd.edu}"

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

if ! curl -sS --max-time 10 \
  -H "Authorization: Bearer $INSIGHTS_LITELLM_API_KEY" \
  "$BASE_URL/v1/models" > "$OUT"; then
  echo "✗ Failed to reach $BASE_URL/v1/models"
  echo "  (from the host use localhost; host.docker.internal resolves only inside containers)"
  exit 1
fi

# Which models must be present is a property of the deployment, not of this
# script. A hardcoded roster was wrong the moment the gateway changed: it
# demanded Anthropic and Cohere ids from a local vLLM that correctly served
# neither. Set ALEPH_REQUIRED_MODELS to assert a specific set.
ALEPH_REQUIRED_MODELS="${ALEPH_REQUIRED_MODELS:-}" \
BASE_URL="$BASE_URL" python3 - "$OUT" <<'PY'
import json
import os
import sys

with open(sys.argv[1]) as f:
    body = json.load(f)
got = {m["id"] for m in body.get("data", [])}

if not got:
    print(f"✗ {os.environ['BASE_URL']} served an empty model list", file=sys.stderr)
    sys.exit(1)

required = {m.strip() for m in os.environ["ALEPH_REQUIRED_MODELS"].split(",") if m.strip()}
missing = required - got
if missing:
    print(f"✗ gateway missing required models: {sorted(missing)}", file=sys.stderr)
    print(f"  serves: {sorted(got)}", file=sys.stderr)
    sys.exit(1)

print(f"✓ gateway reachable; serves {len(got)} model(s): {sorted(got)}")
if not required:
    print("  (set ALEPH_REQUIRED_MODELS to assert a set; acceptance H1 checks the live bindings)")
PY
