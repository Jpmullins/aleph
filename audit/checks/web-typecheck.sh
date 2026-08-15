#!/usr/bin/env bash
# claim: web-typecheck (static)
source "$(dirname "$0")/lib.sh"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

command -v pnpm >/dev/null 2>&1 || skip "pnpm not installed"
[ -d "$ROOT/apps/web/node_modules" ] || skip "apps/web deps not installed"

out=$(cd "$ROOT" && pnpm -C apps/web typecheck 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
  echo "$out" | grep -E "error TS" | head -5
  fail "tsc --noEmit reported type errors (rc=$rc)"
fi
pass "apps/web typechecks clean (tsc --noEmit)"
