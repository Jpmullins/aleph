#!/usr/bin/env bash
# Verify the configured LiteLLM gateway can actually run Aleph.
#
# This used to assert a hardcoded list of five model ids. That passed against the
# gateway it was written for and failed against the next one, where nothing
# matched (`claude-sonnet-4-6` vs `bedrock-claude-sonnet-4-6`) — it only ever
# proved that two copies of the same assumption agreed. A local vLLM, which
# correctly serves neither Anthropic nor Cohere ids, failed it outright.
#
# It now asks the gateway what it serves, applies the same capability policy the
# app uses to pick defaults, and — because an advertised model is not necessarily
# a reachable one — calls each model once. Both reference deployments advertise
# models that fail on invocation ("Model access is denied", a Sonnet needing an
# inference profile); only probing finds them.
#
# Scope: this checks the *gateway*, standalone and without a database. The
# authoritative check — "every model bound in every ModelProfile is served, and
# the embedder's width matches the column" — needs the DB and lives in
# `scripts/_acceptance/gateway_serves_bound_models.py` (acceptance H1). This is
# the fast one you run before booting.
#
# Set ALEPH_REQUIRED_MODELS=a,b,c to additionally assert specific ids, and
# ALEPH_VERIFY_PROBE=0 to list without invoking anything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/compose/.env"

# Fill only what the caller has not already set. `set -a; . .env` would clobber
# an explicit `LITELLM_BASE_URL=... ./scripts/verify-gateway.sh`, which is the
# normal way to point this at a gateway other than the configured one — and it
# overwrites with whatever the file holds, including an *empty* key, which is
# exactly how a correctly-configured shell still reported "not set".
#
# Surrounding quotes are stripped, because sourcing did that for free and this
# loop does not: `KEY="sk-..."` would otherwise be sent as `Bearer "sk-..."`,
# which the gateway rejects and this script reports as unreachable. Compose
# reads the same file with the same rule, so the two agree about it.
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    case "$key" in *[!A-Za-z0-9_]*|'') continue ;; esac
    value="${line#*=}"
    case "$value" in
      \"*\") value="${value#\"}" ; value="${value%\"}" ;;
      \'*\') value="${value#\'}" ; value="${value%\'}" ;;
    esac
    [ -n "${!key:-}" ] || export "$key=$value"
  done < "$ENV_FILE"
fi

if [ -z "${INSIGHTS_LITELLM_API_KEY:-}" ]; then
  echo "✗ INSIGHTS_LITELLM_API_KEY not set; can't verify gateway."
  exit 1
fi

export ALEPH_VERIFY_BASE_URL="${LITELLM_BASE_URL:-https://gateway.insights.arlis.umd.edu}"
export ALEPH_VERIFY_KEY="$INSIGHTS_LITELLM_API_KEY"
export ALEPH_VERIFY_PROBE="${ALEPH_VERIFY_PROBE:-1}"
export ALEPH_REQUIRED_MODELS="${ALEPH_REQUIRED_MODELS:-}"

cd "$ROOT"
uv run --quiet python - <<'PY'
import asyncio
import os
import sys

from aleph_models.discovery import (
    discover_models,
    probe_model,
    select_default_bindings,
    unbound_capabilities,
)

BASE = os.environ["ALEPH_VERIFY_BASE_URL"]
KEY = os.environ["ALEPH_VERIFY_KEY"]
PROBE = os.environ.get("ALEPH_VERIFY_PROBE") == "1"
REQUIRED = {m.strip() for m in os.environ.get("ALEPH_REQUIRED_MODELS", "").split(",") if m.strip()}


async def main() -> int:
    try:
        models = await discover_models(base_url=BASE, api_key=KEY)
    except Exception as exc:  # noqa: BLE001 - a CLI check reports, never raises
        print(f"✗ could not reach {BASE}: {exc}", file=sys.stderr)
        print(
            "  (from the host use localhost; host.docker.internal resolves "
            "only inside containers)",
            file=sys.stderr,
        )
        return 1

    if not models:
        print(f"✗ {BASE} advertises no models", file=sys.stderr)
        return 1

    unreachable: dict[str, str] = {}
    if PROBE:
        errors = await asyncio.gather(
            *[probe_model(base_url=BASE, api_key=KEY, model=m) for m in models]
        )
        unreachable = {m.id: e for m, e in zip(models, errors, strict=True) if e}

    print(f"gateway: {BASE}")
    for m in models:
        status = "unreachable" if m.id in unreachable else "ok"
        price = "unpriced" if not m.is_priced else f"${float(m.input_per_token) * 1e6:.2f}/Mtok in"
        print(f"  {m.id:<30} {str(m.mode):<10} {price:<22} {status}")
    for mid, err in unreachable.items():
        print(f"    ! {mid}: {err.splitlines()[0][:120]}")

    bindings = select_default_bindings(models, unreachable=frozenset(unreachable))
    print("\ncapabilities Aleph can bind on this gateway:")
    for cap, b in bindings.items():
        print(f"  {cap:<16} -> {b['model']}")
    missing = [c.value for c in unbound_capabilities(bindings)]
    if missing:
        print(f"  unbound: {', '.join(missing)}")

    unpriced = [m.id for m in models if not m.is_priced]
    if unpriced:
        print(
            f"\n! {len(unpriced)} model(s) carry no price and would record "
            f"pricing_source=unknown if bound: {', '.join(unpriced)}",
            file=sys.stderr,
        )

    # Which ids must exist is a property of a deployment, not of this script —
    # so it is asserted only when someone asks for it.
    absent = sorted(REQUIRED - {m.id for m in models})
    if absent:
        print(f"\n✗ gateway missing required models: {absent}", file=sys.stderr)
        return 1

    # Aleph cannot run without something to talk to and something to embed with;
    # every other capability degrades rather than stops.
    problems = [c for c in ("synthesis", "embedding") if c not in bindings]
    if problems:
        print(
            f"\n✗ gateway cannot serve required capabilities: {', '.join(problems)}",
            file=sys.stderr,
        )
        return 1

    total = len(bindings) + len(missing)
    print(f"\n✓ {len(models)} models discovered, {len(bindings)}/{total} capabilities bindable")
    if not REQUIRED:
        print("  (acceptance H1 checks the live ModelProfile bindings against this gateway)")
    return 0


raise SystemExit(asyncio.run(main()))
PY
