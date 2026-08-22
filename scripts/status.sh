#!/usr/bin/env bash
#
# The eight numbers that mean done, printed by one command.
#
# `docs/plan.md` Part 1 says success is abstract by nature, so it needs proxies
# that are not, and that six of the eight should be printable by one script.
# This is that script.
#
# The rule it exists to enforce: a number that cannot be computed prints `n/a`
# and says what is missing. It never prints zero. A zero meaning "no defects"
# and a zero meaning "nothing was measured" look identical on a dashboard, and
# this project has already shipped one of those — 0.91 recall against an index
# holding no rows.
#
# Usage:
#   ./scripts/status.sh            # the numbers (runs the full gate; minutes)
#   ./scripts/status.sh --quick    # skip the service-backed checks, and say so
#   ./scripts/status.sh --brief    # one line per number, no explanation
#
# Exits 0 if every measurable number is good, 1 otherwise. A number that is
# `n/a` does not fail the run — it is reported as unmeasured, which is a
# different and honest thing.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BRIEF=0
QUICK=0
for arg in "$@"; do
  case "$arg" in
    --brief) BRIEF=1 ;;
    --quick) QUICK=1 ;;
  esac
done

DB_URL="${DATABASE_URL:-${ALEPH_TEST_DATABASE_URL:-}}"
export DATABASE_URL="$DB_URL"

BAD=0
UNMEASURED=0

bold() { printf '\033[1m%s\033[0m\n' "$*"; }

# line N NAME VALUE VERDICT NOTE
line() {
  local n="$1" name="$2" value="$3" verdict="$4" note="$5" colour
  case "$verdict" in
    ok)      colour='\033[32m' ;;
    FAIL)    colour='\033[31m'; BAD=$((BAD+1)) ;;
    info)    colour='\033[90m' ;;
    unknown) colour='\033[90m'; UNMEASURED=$((UNMEASURED+1)) ;;
    *)       colour='\033[90m' ;;
  esac
  printf "%-3s %-24s ${colour}%-12s %-8s\033[0m %s\n" "$n" "$name" "$value" "$verdict" "$note"
}

bold "Aleph — the eight numbers  ($(git rev-parse --short HEAD 2>/dev/null || echo 'no git'))"
[ $BRIEF -eq 0 ] && echo "  docs/plan.md Part 1. Aleph is finished when all eight hold at once."
echo
printf '%-3s %-24s %-12s %-8s %s\n' "#" "NUMBER" "VALUE" "VERDICT" "MEANS"
printf '%-3s %-24s %-12s %-8s %s\n' "---" "------------------------" "------------" "--------" "-----"

# --- 1. the gate --------------------------------------------------------------
#
# The FULL gate by default, not `--quick`. Quick mode skips every service-backed
# check, so `skip <= 2` is unreachable by construction and number 1 would read
# FAIL forever for a reason that has nothing to do with the system. A fast
# number that cannot be met is the same failure as a green light that means
# nothing, pointed the other way.
#
# It takes a few minutes. That is the honest cost of the definition of done;
# `--quick` is available and is labelled when used.
if [ $QUICK -eq 1 ]; then
  GATE_MODE="--quick (service-backed checks skipped)"
  GATE_OUT="$(./scripts/acceptance.sh --quick 2>&1 | tail -60)"
else
  GATE_MODE="full"
  GATE_OUT="$(./scripts/acceptance.sh 2>&1 | tail -60)"
fi
GATE_LINE="$(printf '%s' "$GATE_OUT" | grep -oE 'pass=[0-9]+ +fail=[0-9]+ +red=[0-9]+.*' | tail -1)"
GATE_FAIL="$(printf '%s' "$GATE_LINE" | grep -oE 'fail=[0-9]+' | cut -d= -f2)"
GATE_SKIP="$(printf '%s' "$GATE_LINE" | grep -oE 'skip=[0-9]+' | cut -d= -f2)"
GATE_MISS="$(printf '%s' "$GATE_LINE" | grep -oE 'missing=[0-9]+' | cut -d= -f2)"
if [ -n "${GATE_FAIL:-}" ]; then
  if [ "$GATE_FAIL" -eq 0 ] && [ "${GATE_MISS:-0}" -eq 0 ] && [ "${GATE_SKIP:-99}" -le 2 ]; then
    line 1 "acceptance gate" "f${GATE_FAIL}/s${GATE_SKIP}/m${GATE_MISS}" ok "fail=0, skip<=2, no missing subjects ($GATE_MODE)"
  else
    line 1 "acceptance gate" "f${GATE_FAIL}/s${GATE_SKIP}/m${GATE_MISS}" FAIL "needs fail=0, skip<=2, missing=0 ($GATE_MODE)"
  fi
else
  line 1 "acceptance gate" "n/a" unknown "could not parse acceptance.sh output"
fi

# --- 2, 3, 5, 6: straight out of the database --------------------------------
# A temp file rather than an associative array: macOS ships bash 3.2, which has
# no `declare -A`, and a status script that only runs on the maintainer's Linux
# box is a status nobody checks.
DB_TSV="$(mktemp)"
trap 'rm -f "$DB_TSV"' EXIT
if [ -n "$DB_URL" ]; then
  uv run --quiet python scripts/_acceptance/status_numbers.py > "$DB_TSV" 2>/dev/null
fi
dbline() { # dbline N LABEL KEY
  local key="$3" row
  row="$(grep -m1 "^${key}	" "$DB_TSV" 2>/dev/null)"
  if [ -n "$row" ]; then
    local value verdict note
    value="$(printf '%s' "$row" | cut -f2)"
    verdict="$(printf '%s' "$row" | cut -f3)"
    note="$(printf '%s' "$row" | cut -f4)"
    line "$1" "$2" "$value" "$verdict" "$note"
  else
    line "$1" "$2" "n/a" unknown "no reachable database (set DATABASE_URL)"
  fi
}

# Number 2 has two halves: the eval score, and a non-empty index in the same
# database. Only the second is computable today — the eval set is 45 questions
# and reports recall only, so nDCG@10 does not exist. Reported as such.
dbline 2a "retrieval index rows" chunks
if [ -x "$(command -v uv)" ] && grep -q 'ndcg' packages/aleph-evals/src/aleph_evals/retrieval_eval.py 2>/dev/null; then
  line 2b "retrieval nDCG@10" "see eval" unknown "run: uv run python -m aleph_evals.retrieval_eval"
else
  line 2b "retrieval nDCG@10" "n/a" unknown "no nDCG in the eval, and the set is 45 questions (WS-RS5)"
fi

dbline 3 "ungrounded citations" ungrounded_citations
dbline 3b "  ...in test fixtures" ungrounded_citations_fixtures
dbline 5 "uncosted model calls" uncosted_model_calls
dbline 5b "  ...all time (D9)" uncosted_model_calls_legacy
dbline 6 "stuck agent runs" stuck_agent_runs

# --- 4. the agent authors a skill that survives -------------------------------
if [ -f tests/integration/test_authored_skills.py ]; then
  if uv run --quiet pytest -m integration tests/integration/test_authored_skills.py -q -p no:randomly >/dev/null 2>&1; then
    line 4 "authored skill survives" "pass" ok "a skill written in one thread is visible in another"
  else
    line 4 "authored skill survives" "fail" FAIL "tests/integration/test_authored_skills.py is red"
  fi
else
  line 4 "authored skill survives" "n/a" unknown "the skills backend is read-only (WS-H1)"
fi

# --- 7. p95 first-token latency ----------------------------------------------
#
# Deliberately not measured here. The probe drives REAL chat turns against a
# real gateway, so it takes a minute and spends tokens, and `status.sh` is meant
# to be something you run without thinking about it. Naming the command is more
# useful than a stale cached number, which is the other way this line could go.
#
# It stays `unknown` even once the probe exists, and that is not a hedge: Part 1
# states no ceiling for number 7, so there is nothing for a measurement to be
# compared against yet. A verdict needs a threshold; without one, a number is
# just a number.
if [ -f scripts/_acceptance/agent_turn_probe.py ]; then
  line 7 "p95 first-token" "see probe" unknown \
    "no stated ceiling; measure with: uv run python scripts/_acceptance/agent_turn_probe.py"
else
  line 7 "p95 first-token" "n/a" unknown "no probe exists and no ceiling is stated (WS-E1c)"
fi

# --- 8. dead code by construction --------------------------------------------
WEB_FILES="$(git ls-files apps/web/src 2>/dev/null | wc -l | tr -d ' ')"
SWEEPS_OK=1
for sweep in check-catalog-generated check-graph-state-keys check-surface-bindings \
             check-single-catalog check-pane-registry check-dead-refs; do
  [ -x "scripts/$sweep.sh" ] || continue
  "./scripts/$sweep.sh" >/dev/null 2>&1 || SWEEPS_OK=0
done
if [ -x scripts/check-web-dead-code.sh ]; then
  ./scripts/check-web-dead-code.sh >/dev/null 2>&1 || SWEEPS_OK=0
  if [ $SWEEPS_OK -eq 1 ]; then
    line 8 "dead code" "${WEB_FILES} web files" ok "every sweep exits 0"
  else
    line 8 "dead code" "${WEB_FILES} web files" FAIL "a sweep is red"
  fi
elif [ $SWEEPS_OK -eq 1 ]; then
  line 8 "dead code" "${WEB_FILES} web files" unknown "no web reachability sweep yet (WS-UI-1)"
else
  line 8 "dead code" "${WEB_FILES} web files" FAIL "a sweep is red"
fi

echo
if [ $BAD -gt 0 ]; then
  bold "$BAD number(s) failing, $UNMEASURED not yet measurable."
  exit 1
fi
bold "0 failing, $UNMEASURED not yet measurable."
[ $UNMEASURED -gt 0 ] && echo "Not measurable is not the same as good. Each one names what is missing."
exit 0
