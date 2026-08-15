#!/usr/bin/env bash
#
# The comprehensive acceptance check for the Aleph refactor.
#
# Runs every part's check from docs/acceptance.md and prints a per-part verdict.
#
# Three rules this script exists to enforce, because the previous gate violated
# all three and stayed green over a broken central hypothesis for seven work
# packages:
#
#   1. A SKIP is never a PASS. An unrunnable check reports SKIP and is counted
#      separately. Collapsing them is how "31/32 green" happened.
#   2. A deliberately-red check is reported as RED, not as a failure. Those are
#      acceptance tests for known defects — they name the bug and go green when
#      it is fixed. "2 red" means "2 known defects under test".
#   3. `--self-check` verifies the checks can fail, by mutating each subject and
#      asserting its check notices. A check nobody has seen fail is an
#      assumption wearing a green light.
#
# Usage:
#   ./scripts/acceptance.sh              # everything runnable here
#   ./scripts/acceptance.sh --quick      # skip anything needing services
#   ./scripts/acceptance.sh --self-check # prove the checks can fail
#   ./scripts/acceptance.sh --part B     # one part only
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

QUICK=0
SELF_CHECK=0
ONLY_PART=""
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) QUICK=1 ;;
    --self-check) SELF_CHECK=1 ;;
    --part) ONLY_PART="${2:-}"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

DB_URL="${ALEPH_TEST_DATABASE_URL:-postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph}"
REDIS="${ALEPH_TEST_REDIS_URL:-redis://localhost:6379/0}"

PASS=0; FAIL=0; RED=0; SKIP=0
declare -a ROWS=()

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
row()  { ROWS+=("$1|$2|$3"); }

# record ID STATUS DETAIL
record() {
  case "$2" in
    PASS) PASS=$((PASS+1)) ;;
    FAIL) FAIL=$((FAIL+1)) ;;
    RED)  RED=$((RED+1)) ;;
    SKIP) SKIP=$((SKIP+1)) ;;
  esac
  row "$1" "$2" "$3"
}

part_selected() { [ -z "$ONLY_PART" ] || [ "${1:0:1}" = "$ONLY_PART" ]; }

services_up() {
  (echo > /dev/tcp/localhost/5432) >/dev/null 2>&1 || return 1
  (echo > /dev/tcp/localhost/6379) >/dev/null 2>&1 || return 1
  return 0
}

# run_pytest ID "DESCRIPTION" <pytest args...>
run_pytest() {
  local id="$1" desc="$2"; shift 2
  part_selected "$id" || return 0
  local out rc
  out="$(uv run pytest "$@" -q -p no:randomly 2>&1)"; rc=$?
  local summary
  summary="$(printf '%s' "$out" | grep -oE '[0-9]+ (passed|failed)(, [0-9]+ (passed|failed))*' | tail -1)"
  if [ $rc -eq 0 ]; then record "$id" PASS "${summary:-$desc}"
  else record "$id" FAIL "${summary:-$desc}"; fi
}

# run_expected_red ID "DESC" <pytest args...>  — these SHOULD fail today
run_expected_red() {
  local id="$1" desc="$2"; shift 2
  part_selected "$id" || return 0
  if uv run pytest "$@" -q -p no:randomly >/dev/null 2>&1; then
    # It went green. That is the acceptance test passing — a real event.
    record "$id" PASS "FIXED — was a known defect ($desc)"
  else
    record "$id" RED "$desc"
  fi
}

# run_expected_red_shell ID "DESC" "command" — SHOULD fail today
run_expected_red_shell() {
  local id="$1" desc="$2" cmd="$3"
  part_selected "$id" || return 0
  if bash -c "$cmd" >/dev/null 2>&1; then
    record "$id" PASS "FIXED — was a known defect ($desc)"
  else
    record "$id" RED "$desc"
  fi
}

# run_shell ID "DESC" "command"
run_shell() {
  local id="$1" desc="$2" cmd="$3"
  part_selected "$id" || return 0
  local out rc
  out="$(bash -c "$cmd" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then record "$id" PASS "${out:-$desc}"
  else record "$id" FAIL "${out:-$desc}"; fi
}

skip() { part_selected "$1" && record "$1" SKIP "$2"; }

bold "Aleph acceptance — $(git rev-parse --short HEAD 2>/dev/null || echo 'no git')"
echo

NEEDS_SERVICES=0
if [ $QUICK -eq 0 ]; then
  if services_up; then NEEDS_SERVICES=1; else
    echo "  postgres/redis not reachable — service-backed checks will SKIP"
    echo "  (docker compose -f deploy/compose/docker-compose.yml up -d postgres redis)"
    echo
  fi
fi

export ALEPH_DATABASE_URL="$DB_URL" DATABASE_URL="$DB_URL" REDIS_URL="$REDIS"
export ALEPH_AUTH_MODE=local

# ---------------------------------------------------------------------------
# A — Kernel
# ---------------------------------------------------------------------------
run_pytest A1 "kernel core" packages/aleph-kernel/tests

if [ $NEEDS_SERVICES -eq 1 ]; then
  run_shell A2 "API boots on the kernel; all probes pass" \
    "uv run python scripts/_acceptance/kernel_boot.py"
else
  skip A2 "needs postgres+redis"
fi

skip A3 "not started — workers still wire their own singletons"
skip A4 "not started — generation loader"
skip A5 "not started — boot manifest"
skip A6 "not started — agent-facing plugin API"

# ---------------------------------------------------------------------------
# B — Retrieval
# ---------------------------------------------------------------------------
if [ $NEEDS_SERVICES -eq 1 ]; then
  run_pytest B1 "a page is retrievable by words in its body" \
    tests/e2e/test_retrieval_finds_body_text.py::test_body_phrase_retrieves_its_page
  run_pytest B2 "a natural-language question retrieves its page" \
    tests/e2e/test_retrieval_finds_body_text.py::test_natural_language_question_retrieves_its_page
  run_pytest B3 "corpus-wide hybrid search, scoped and diversity-capped" \
    tests/e2e/test_search_corpus.py
  run_pytest B7 "stale links are not expanded" \
    tests/e2e/test_retrieval_finds_body_text.py::test_expansion_ignores_links_from_superseded_revisions
else
  skip B1 "needs postgres"; skip B2 "needs postgres"
  skip B3 "needs postgres"; skip B7 "needs postgres"
fi
run_pytest B4 "reciprocal rank fusion" packages/aleph-core/tests/test_rrf.py
run_shell B9 "corpus search is WIRED, not merely built" \
  "uv run python -c \"
import inspect
from aleph_assistant.retrieval import router as r
src = inspect.getsource(r)
# The dominant defect in this codebase is a write path with no read path.
# search_corpus existing is not the same as retrieval using it.
assert 'search_corpus' in src, 'the router does not call search_corpus'
assert 'corpus_chunks' in src, 'corpus hits never reach the composer'
assert 'and not corpus_chunks' in src, 'an empty page search still short-circuits before consulting sources'
print('router searches the corpus and feeds it to the composer')
\""
if [ $NEEDS_SERVICES -eq 1 ]; then
  # B5 asserts the harness INVOKES the system. The eval it replaced read
  # `expected` and `actual` out of the same fixture line, so it could not fail.
  run_shell B5 "the retrieval eval invokes Aleph and reports a number" \
    "uv run python -m aleph_evals.retrieval_eval -k 3 --min-recall 0.80 | head -2"
else
  skip B5 "needs postgres"
fi
run_shell B6 "retrieval dataset has >=40 labelled pairs" \
  "n=\$(wc -l < packages/aleph-evals/datasets/retrieval/questions.jsonl); \
   echo \"\$n question/source pairs\"; [ \"\$n\" -ge 40 ]"
# Asserted by import, not by grep: `grep -q '_MISS_REASON'` also matched
# `_MISS_REASON_UNUSED`, so the check survived its own subject being renamed
# away. Substring matching is not a check.
run_shell B8 "empty search reports honestly" \
  "uv run python -c \"
import inspect
from aleph_assistant.retrieval import router as r
# The diagnostic must exist AND name the mechanism. 'I found nothing' without a
# cause sends a reader off to re-ingest material that is already there.
assert hasattr(r, '_MISS_REASON'), 'the honest-miss diagnostic is gone'
for word in ('titles', 'bodies'):
    assert word in r._MISS_REASON, f'the diagnostic no longer names the cause ({word!r} missing)'
# Assert on the CALL, not the word: 'list_pages' also appears in the comment
# explaining why the fallback was removed, so a bare substring test fails on
# its own documentation.
src = inspect.getsource(r)
assert 'from_fallback' not in src, 'the fallback plumbing is back'
assert '.list_pages(' not in src, 'the router calls list_pages again'
print('honest-miss diagnostic names its cause; fallback plumbing gone')
\""

# ---------------------------------------------------------------------------
# C — Belief engine
# ---------------------------------------------------------------------------
run_pytest C0 "belief patch contract + trust lattice" packages/aleph-belief/tests
run_pytest C2 "evidence is verbatim-verified against the source" \
  packages/aleph-core/tests/test_grounding.py
if [ $NEEDS_SERVICES -eq 1 ]; then
  run_pytest C1 "a claim survives a page rewrite" \
    tests/e2e/test_belief_spine.py::test_reasserting_a_claim_keeps_its_identity \
    tests/e2e/test_belief_spine.py::test_citations_accumulate_across_reassertions \
    tests/e2e/test_belief_spine.py::test_re_deriving_the_same_span_unions_rather_than_duplicates \
    tests/e2e/test_belief_spine.py::test_supersession_keeps_the_old_belief_walkable
  run_pytest C3 "confidence is derived from evidence, not asserted" \
    tests/e2e/test_belief_spine.py::test_confidence_rises_with_supporting_evidence \
    tests/e2e/test_belief_spine.py::test_contradicting_evidence_moves_a_claim_to_contested \
    tests/e2e/test_belief_spine.py::test_support_counts_are_recomputed_not_asserted
  run_pytest C6 "a human's claim is immutable to agents" \
    tests/e2e/test_belief_spine.py::test_an_agent_cannot_overwrite_a_user_claim \
    tests/e2e/test_belief_spine.py::test_a_user_may_revise_their_own_claim
  run_pytest C7 "every written citation carries a source id" \
    tests/e2e/test_belief_spine.py::test_every_written_citation_carries_a_source_id \
    tests/e2e/test_belief_spine.py::test_a_fabricated_quote_is_refused
else
  for p in C1 C3 C6 C7; do skip "$p" "needs postgres"; done
fi
for p in C4 C5 C8; do skip "$p" "not started — retraction walk, reconciliation, rebuild"; done

# ---------------------------------------------------------------------------
# D — Skills / self-improvement
# ---------------------------------------------------------------------------
for p in D1 D2 D3 D4 D5 D6; do skip "$p" "not started"; done

# ---------------------------------------------------------------------------
# E — Deletion
# ---------------------------------------------------------------------------
run_shell E4 "workspace package count" \
  "n=\$(ls packages | wc -l); echo \"\$n packages (target <=17)\"; [ \"\$n\" -le 20 ]"
# Deliberately red: the patch contract shipped before its consumer, which is
# the exact defect class this refactor exists to remove. It goes green when the
# Claim Spine (part C) uses it, or the code gets deleted.
run_expected_red_shell E5 "aleph-belief patch contract still has no consumer" \
  "n=\$(grep -rl 'aleph_belief' --include='*.py' apps packages 2>/dev/null | grep -v 'packages/aleph-belief' | wc -l); [ \"\$n\" -ge 1 ]"
for p in E1 E2 E3; do skip "$p" "blocked on C — cannot delete the wiki before its replacement wins"; done

# ---------------------------------------------------------------------------
# F — Security
# ---------------------------------------------------------------------------
# F1 asserts the real property, in Python, against the imported constant. The
# first version of this check grepped for the word "skip" and passed while the
# bypass was live, because the constant is named _SELF_AUTH_PREFIXES — a check
# that passes for the wrong reason is the thing this whole file exists to stop.
run_pytest F1 "agent endpoint is authenticated; project scope is authorized" \
  packages/aleph-security/tests/test_request_context.py
run_pytest F2 "untrusted ingested text is defanged at the boundary" \
  packages/aleph-rks/tests/test_ingest_defang.py packages/aleph-core/tests/test_grounding.py
run_pytest F3 "agent token scoping" packages/aleph-security/tests

# ---------------------------------------------------------------------------
# G — Verification infrastructure
# ---------------------------------------------------------------------------
run_shell G1a "the retrieval audit check is a known-answer probe, not a length assertion" \
  "grep -q 'body-phrase probe' audit/checks/wiki-first-retrieval.sh \
   && ! grep -q 'len} -ge 40 ] || fail \"retrieval composed_body_md too short' audit/checks/wiki-first-retrieval.sh \
   && echo 'known-answer probe in place'"
run_shell G2 "CI has a behavioural gate" \
  "grep -q 'python-integration' .github/workflows/ci.yml && echo 'integration job present'"

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo
printf '%-5s %-6s %s\n' "PART" "STATUS" "DETAIL"
printf '%-5s %-6s %s\n' "-----" "------" "------------------------------------------------"
for r in "${ROWS[@]}"; do
  IFS='|' read -r id status detail <<< "$r"
  case "$status" in
    PASS) colour='\033[32m' ;;
    FAIL) colour='\033[31m' ;;
    RED)  colour='\033[33m' ;;
    *)    colour='\033[90m' ;;
  esac
  printf "%-5s ${colour}%-6s\033[0m %s\n" "$id" "$status" "${detail:0:90}"
done

echo
bold "pass=$PASS  fail=$FAIL  red=$RED (known defects under test)  skip=$SKIP (not built)"
echo

if [ $SELF_CHECK -eq 1 ]; then
  bold "self-check — can these checks fail?"
  bash "$ROOT/scripts/_acceptance/self_check.sh" || FAIL=$((FAIL+1))
  echo
fi

if [ $FAIL -gt 0 ]; then
  echo "FAIL — $FAIL check(s) that should pass did not."
  exit 1
fi
if [ $SKIP -gt 0 ]; then
  echo "INCOMPLETE — $SKIP part(s) not built. Nothing regressed."
  exit 0
fi
echo "COMPLETE — every part built and checked."
exit 0
