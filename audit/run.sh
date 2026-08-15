#!/usr/bin/env bash
# Aleph intent-audit harness.
#   1. Ensures the app is running (respects existing bootstrap / compose scripts).
#   2. Runs every check under audit/checks/ (one per claim in audit/claims.yaml).
#   3. Writes audit/scorecard.json {id: {result, reason}} + prints a summary table
#      and an overall intent-coverage ratio (claims passing / total).
#
# Check exit-code contract: 0=pass, 1=fail, 2=skip (infra/precondition), other=error.
# e2e claims run as Playwright specs (audit/checks/e2e/<id>.spec.ts); every other
# claim runs as audit/checks/<id>.sh.
set -uo pipefail

AUDIT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$AUDIT_DIR/.." && pwd)"
CHECKS="$AUDIT_DIR/checks"
E2E="$CHECKS/e2e"
CLAIMS="$AUDIT_DIR/claims.yaml"
SCORECARD="$AUDIT_DIR/scorecard.json"
RESULTS="$(mktemp)"

API="${ALEPH_API_BASE_URL:-http://localhost:8000}"
WEB="${ALEPH_WEB_BASE_URL:-http://localhost:5173}"
export ALEPH_API_BASE_URL="$API" ALEPH_WEB_BASE_URL="$WEB"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. Ensure the app is up.
# ---------------------------------------------------------------------------
say "① Preflight — ensuring the Aleph stack is up"
api_up() { curl -fsS -o /dev/null --max-time 5 "$API/healthz" 2>/dev/null; }

if api_up; then
  echo "  API healthy at $API"
else
  echo "  API not responding — attempting to start the stack"
  if [ -f "$ROOT/deploy/compose/.env" ] && [ -x "$ROOT/scripts/bootstrap-local.sh" ]; then
    ( cd "$ROOT" && ./scripts/bootstrap-local.sh ) || true
  elif [ -f "$ROOT/deploy/compose/docker-compose.yml" ]; then
    docker compose -f "$ROOT/deploy/compose/docker-compose.yml" up -d || true
  fi
  for _ in $(seq 1 60); do api_up && break; sleep 2; done
  api_up && echo "  API is now up" || echo "  WARNING: API still down — API-backed checks will skip/fail"
fi

WEB_UP=0
curl -fsS -o /dev/null --max-time 5 "$WEB" 2>/dev/null && WEB_UP=1
echo "  Web ($WEB): $([ $WEB_UP = 1 ] && echo reachable || echo DOWN — e2e will skip)"

# Ensure the e2e node_modules symlink exists (Playwright + @playwright/test).
if [ ! -e "$E2E/node_modules" ] && [ -d "$ROOT/tests/playwright/node_modules" ]; then
  ln -sfn "$ROOT/tests/playwright/node_modules" "$E2E/node_modules"
fi
E2E_OK=0
[ $WEB_UP = 1 ] && [ -e "$E2E/node_modules/.bin/playwright" ] && E2E_OK=1

# ---------------------------------------------------------------------------
# 2. Run each claim's check.
# ---------------------------------------------------------------------------
say "② Running checks (one per claim)"

# Parse "id check_type" pairs from claims.yaml (order preserved).
mapfile -t PAIRS < <(awk '/^- id:/{id=$3} /check_type:/{print id, $2}' "$CLAIMS")

run_sh_check() {  # $1=id -> echoes "result|reason", sets nothing
  local id="$1" file="$CHECKS/$1.sh"
  [ -f "$file" ] && return_run "$file" || echo "error|no check file audit/checks/$id.sh"
}
return_run() {
  local out rc reason
  out="$(bash "$1" 2>&1)"; rc=$?
  reason="$(echo "$out" | grep '^RESULT:' | tail -1 | sed -E 's/^RESULT:(PASS|FAIL|SKIP) ?//')"
  [ -z "$reason" ] && reason="$(echo "$out" | tail -1 | head -c 200)"
  case $rc in
    0) echo "pass|$reason" ;;
    1) echo "fail|$reason" ;;
    2) echo "skip|$reason" ;;
    *) echo "error|exit $rc: $reason" ;;
  esac
}

run_e2e_check() {  # $1=id
  local id="$1" spec="$E2E/$1.spec.ts"
  [ -f "$spec" ] || { echo "error|no e2e spec audit/checks/e2e/$id.spec.ts"; return; }
  [ $E2E_OK = 1 ] || { echo "skip|web/browser unavailable for e2e"; return; }
  local out rc strip
  strip() { sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g; s/\r//g; s/^[[:space:]]+//'; }
  out="$(cd "$E2E" && npx playwright test "$id" --reporter=line 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    echo "pass|$(echo "$out" | grep -E '[0-9]+ passed' | tail -1 | strip | head -c 120)"
  else
    echo "fail|$(echo "$out" | grep -iE 'Error:|expect|[0-9]+ failed' | head -1 | strip | head -c 180)"
  fi
}

: > "$RESULTS"
for pair in "${PAIRS[@]}"; do
  id="${pair%% *}"; ctype="${pair##* }"
  printf '  %-28s [%s] … ' "$id" "$ctype"
  if [ "$ctype" = "e2e" ]; then rr="$(run_e2e_check "$id")"; else rr="$(run_sh_check "$id")"; fi
  result="${rr%%|*}"; reason="${rr#*|}"
  case "$result" in
    pass)  printf '\033[32mPASS\033[0m\n' ;;
    fail)  printf '\033[31mFAIL\033[0m — %s\n' "$reason" ;;
    skip)  printf '\033[33mSKIP\033[0m — %s\n' "$reason" ;;
    *)     printf '\033[35mERROR\033[0m — %s\n' "$reason" ;;
  esac
  printf '%s\t%s\t%s\t%s\n' "$id" "$ctype" "$result" "$reason" >> "$RESULTS"
done

# ---------------------------------------------------------------------------
# 3. Scorecard + summary.
# ---------------------------------------------------------------------------
say "③ Scorecard"
python3 - "$RESULTS" "$SCORECARD" <<'PY'
import json, sys
rows = []
with open(sys.argv[1]) as f:
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4: continue
        cid, ctype, result, reason = parts[0], parts[1], parts[2], parts[3]
        rows.append((cid, ctype, result, reason))

scorecard = {cid: {"check_type": ctype, "result": result, "reason": reason}
             for cid, ctype, result, reason in rows}
with open(sys.argv[2], "w") as f:
    json.dump(scorecard, f, indent=2)

counts = {"pass":0,"fail":0,"skip":0,"error":0}
for _,_,r,_ in rows: counts[r] = counts.get(r,0)+1

width = max((len(c) for c,_,_,_ in rows), default=20)
print(f"\n  {'CLAIM'.ljust(width)}  TYPE      RESULT")
print(f"  {'-'*width}  --------  ------")
sym = {"pass":"PASS","fail":"FAIL","skip":"SKIP","error":"ERR "}
for cid, ctype, result, reason in rows:
    print(f"  {cid.ljust(width)}  {ctype.ljust(8)}  {sym.get(result,result)}")

total = len(rows)
passed = counts["pass"]
# Coverage = passing / total claims. Skips/fails/errors all count against coverage.
ratio = (passed/total) if total else 0.0
print(f"\n  totals: {passed} pass · {counts['fail']} fail · {counts['skip']} skip · {counts['error']} error  (of {total})")
print(f"  INTENT COVERAGE: {passed}/{total} = {ratio:.0%}  (claims whose check actually asserted the intended outcome)")
print(f"\n  scorecard written to {sys.argv[2]}")

if counts["fail"] or counts["error"]:
    print("\n  Non-passing claims:")
    for cid, ctype, result, reason in rows:
        if result in ("fail","error"):
            print(f"    [{result.upper()}] {cid}: {reason}")
    if counts["skip"]:
        print("\n  Skipped (infra/precondition, not a code verdict):")
        for cid, ctype, result, reason in rows:
            if result == "skip":
                print(f"    [SKIP] {cid}: {reason}")
PY

rm -f "$RESULTS"
