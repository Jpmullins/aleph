#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/compose/.env"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

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
  exit 1
fi

python3 - "$OUT" <<'PY'
import json
import sys

required = {
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "cohere-embed-v4",
    "cohere-embed-english-v3",
}

with open(sys.argv[1]) as f:
    body = json.load(f)
got = {m["id"] for m in body.get("data", [])}
missing = required - got
if missing:
    print(f"✗ LiteLLM gateway missing required models: {sorted(missing)}", file=sys.stderr)
    sys.exit(1)
print(f"✓ gateway has {len(got)} models; all required present")
PY
