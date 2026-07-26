#!/usr/bin/env bash
# Verify the configured LiteLLM gateway can actually run Aleph.
#
# This used to assert a hardcoded list of five model ids. That passed against the
# gateway it was written for and failed against the next one, where nothing
# matched (`claude-sonnet-4-6` vs `bedrock-claude-sonnet-4-6`) — it only ever
# proved that two copies of the same assumption agreed.
#
# It now asks the gateway what it serves, applies the same capability policy the
# app uses to pick defaults, and — because an advertised model is not necessarily
# a reachable one — calls each model once. Both reference deployments advertise
# models that fail on invocation ("Model access is denied", a Sonnet needing an
# inference profile); only probing finds them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/compose/.env"

# An explicitly exported value must win over the file. Sourcing `.env` with
# `set -a` otherwise overwrites the caller's environment with whatever the file
# holds — including an *empty* key, which is exactly how a correctly-configured
# shell still reported "INSIGHTS_LITELLM_API_KEY not set".
_pre_key="${INSIGHTS_LITELLM_API_KEY:-}"
_pre_url="${LITELLM_BASE_URL:-}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
[ -n "$_pre_key" ] && INSIGHTS_LITELLM_API_KEY="$_pre_key"
[ -n "$_pre_url" ] && LITELLM_BASE_URL="$_pre_url"

if [ -z "${INSIGHTS_LITELLM_API_KEY:-}" ]; then
  echo "✗ INSIGHTS_LITELLM_API_KEY not set; can't verify gateway."
  exit 1
fi

export ALEPH_VERIFY_BASE_URL="${LITELLM_BASE_URL:-https://gateway.insights.arlis.umd.edu}"
export ALEPH_VERIFY_KEY="$INSIGHTS_LITELLM_API_KEY"
export ALEPH_VERIFY_PROBE="${ALEPH_VERIFY_PROBE:-1}"

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


async def main() -> int:
    try:
        models = await discover_models(base_url=BASE, api_key=KEY)
    except Exception as exc:  # noqa: BLE001 - a CLI check reports, never raises
        print(f"✗ could not read {BASE}/model/info: {exc}", file=sys.stderr)
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
    return 0


raise SystemExit(asyncio.run(main()))
PY
