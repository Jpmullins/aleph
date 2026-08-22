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
# And a fourth, added after the gate itself drifted into being the thing it was
# built to prevent:
#
#   4. MISSING is not SKIP, and it is always fatal. SKIP means "this machine
#      cannot run this check". MISSING means "the subject of this check is
#      gone". Twenty invocations here named test files deleted in the harness
#      reset; with services down they reported SKIP, and the tree looked green
#      over eleven parts that could not run at all. The preflight below resolves
#      every path this script names, before any check runs and regardless of
#      whether anything is up.
#
# Usage:
#   ./scripts/acceptance.sh              # everything runnable here
#   ./scripts/acceptance.sh --quick      # skip anything needing services
#   ./scripts/acceptance.sh --self-check # prove the checks can fail
#   ./scripts/acceptance.sh --part B     # one part only
#   ./scripts/acceptance.sh --strict     # a SKIP is a failure (for CI, where
#                                        # services are guaranteed)
#   ./scripts/acceptance.sh --max-skip N # fail if more than N parts skip. The
#                                        # budget only ever ratchets DOWN.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

QUICK=0
SELF_CHECK=0
STRICT=0
MAX_SKIP=-1
ONLY_PART=""
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) QUICK=1 ;;
    --self-check) SELF_CHECK=1 ;;
    --strict) STRICT=1 ;;
    --max-skip) MAX_SKIP="${2:-0}"; shift ;;
    --part) ONLY_PART="${2:-}"; shift ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

DB_URL="${ALEPH_TEST_DATABASE_URL:-postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph}"
REDIS="${ALEPH_TEST_REDIS_URL:-redis://localhost:6379/0}"

PASS=0; FAIL=0; RED=0; SKIP=0; MISSING=0
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
    MISS) MISSING=$((MISSING+1)) ;;
  esac
  row "$1" "$2" "$3"
}

part_selected() { [ -z "$ONLY_PART" ] || [ "${1:0:1}" = "$ONLY_PART" ]; }

# Read the host and port out of the URLs the checks will actually use. This
# probed localhost:5432 unconditionally, so a developer running the normal dev
# stack — Postgres published on 5442 — got a screen of SKIPs and a green exit
# while nothing at all had been verified.
url_host_port() { # url_host_port URL DEFAULT_PORT
  local url="$1" default="$2" hostport
  hostport="${url#*://}"          # strip scheme
  hostport="${hostport#*@}"       # strip credentials, if any
  hostport="${hostport%%/*}"      # strip path
  case "$hostport" in
    *:*) echo "${hostport%%:*} ${hostport##*:}" ;;
    *)   echo "$hostport $default" ;;
  esac
}

services_up() {
  local h p
  read -r h p <<< "$(url_host_port "$DB_URL" 5432)"
  (echo > "/dev/tcp/$h/$p") >/dev/null 2>&1 || return 1
  read -r h p <<< "$(url_host_port "$REDIS" 6379)"
  (echo > "/dev/tcp/$h/$p") >/dev/null 2>&1 || return 1
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
  # The LAST line, not the first: a script that prints its verdict at the end
  # would otherwise be reported by whatever warning a library emitted first.
  local last; last="$(printf '%s' "$out" | grep -v '^\s*$' | tail -1)"
  if [ $rc -eq 0 ]; then record "$id" PASS "${last:-$desc}"
  else record "$id" FAIL "${last:-$desc}"; fi
}

skip() { part_selected "$1" && record "$1" SKIP "$2"; }

# ---------------------------------------------------------------------------
# Preflight — does every subject this script names still exist?
# ---------------------------------------------------------------------------
#
# Runs before any check and independently of whether services are up, because
# that is the exact combination that hid the drift: with Postgres down, eleven
# parts naming four deleted test files reported SKIP and the run exited 0.
#
# A path this script cannot resolve is MISSING, not SKIP, and MISSING always
# fails the run. There is no flag to soften it — a warning is how this started.
declare -a MISSING_PATHS=()
preflight() {
  local token path
  while IFS= read -r token; do
    [ -n "$token" ] || continue
    path="${token%%::*}"
    [ -e "$path" ] && continue
    MISSING_PATHS+=("$token")
  done < <(
    grep -ohE '(tests|packages|apps|scripts|audit|docs)/[A-Za-z0-9_./+-]+(::[A-Za-z0-9_]+)?' "$0" \
      | sed 's/[.,)"'"'"']*$//' \
      | sort -u
  )
}

bold "Aleph acceptance — $(git rev-parse --short HEAD 2>/dev/null || echo 'no git')"
echo

preflight
if [ ${#MISSING_PATHS[@]} -gt 0 ]; then
  bold "MISSING subjects — this gate names things that are not there:"
  for m in "${MISSING_PATHS[@]}"; do
    printf '  \033[31m%-8s\033[0m %s\n' "MISSING" "$m"
    record "PRE" MISS "$m"
  done
  echo
fi

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

# A3 boots the WORKER's manifest, which mounts a second Redis for the sandbox
# bus. `CODE_RUNNER_REDIS_URL` defaults to a compose hostname (`runner-redis`),
# so a developer whose environment carries the compose `.env` gets a name that
# resolves only inside the docker network. That is "cannot run here", not
# "broken" — and reporting it as FAIL is how a gate teaches people to ignore
# its colours.
code_runner_bus_up() {
  local url="${CODE_RUNNER_REDIS_URL:-redis://localhost:6379/1}" h p
  read -r h p <<< "$(url_host_port "$url" 6379)"
  (echo > "/dev/tcp/$h/$p") >/dev/null 2>&1
}

if [ $NEEDS_SERVICES -eq 1 ] && code_runner_bus_up; then
  run_shell A3 "workers boot on the kernel; no duplicated wiring" \
    "uv run python scripts/_acceptance/worker_boot.py"
elif [ $NEEDS_SERVICES -eq 1 ]; then
  skip A3 "the sandbox bus at ${CODE_RUNNER_REDIS_URL:-redis://localhost:6379/1} is not reachable from here"
else
  skip A3 "needs postgres+redis"
fi
run_pytest A4 "a live plugin is replaceable, and a failed swap rolls back" \
  packages/aleph-kernel/tests/test_replace.py
run_pytest A5 "boot manifest is the only source of protected capability" \
  packages/aleph-kernel/tests/test_manifest.py
run_pytest A6 "agent plugin API: install, disable, and the addressability guard" \
  packages/aleph-kernel/tests/test_agent_api.py

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
# The diagnostic must name BOTH surfaces it searched, or a reader cannot tell
# whether 'nothing found' means the wiki is empty or the sources are.
for word in ('page', 'bodies', 'source'):
    assert word in r._MISS_REASON, f'the diagnostic no longer names the cause ({word!r} missing)'
# And it must not describe a defect that was fixed: retrieval covers page bodies
# and ORs its terms. A diagnostic that outlived its defect sends people to fix
# the wrong thing.
for stale in ('never page bodies', 'requires every term'):
    assert stale not in r._MISS_REASON, f'the diagnostic still describes the OLD behaviour: {stale!r}'
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
  run_pytest C4 "retraction propagates, with a declined branch" \
    tests/e2e/test_retraction_walk.py
  run_pytest C5 "reconciliation is deterministic and proposes, never applies" \
    packages/aleph-belief/tests/test_reconcile.py \
    tests/e2e/test_belief_spine.py::test_duplicate_beliefs_are_proposed_for_merge_without_a_model
else
  for p in C1 C3 C4 C5 C6 C7; do skip "$p" "needs postgres"; done
fi
# C9 is in part C because `commit_revision` IS the claim-write path: every claim
# in the database today was written through this function, so a commit it loses
# is claims it loses.
run_shell C9a "no unlocked wiki page read in the create-or-lock path" \
  "./scripts/check-page-lock.sh"
if [ $NEEDS_SERVICES -eq 1 ]; then
  run_pytest C9 "concurrent wiki commits do not lose work" \
    tests/integration/test_commit_revision_concurrency.py
else
  skip C9 "needs postgres"
fi

if [ $NEEDS_SERVICES -eq 1 ]; then
  run_pytest C8 "the belief graph rebuilds from its sources, idempotently" \
    tests/e2e/test_belief_spine.py::test_the_belief_graph_rebuilds_from_its_sources \
    tests/e2e/test_belief_spine.py::test_rebuilding_twice_is_idempotent \
    tests/e2e/test_belief_spine.py::test_a_rebuild_does_not_destroy_human_corrections
else
  skip C8 "needs postgres"
fi

# ---------------------------------------------------------------------------
# D — Skills / self-improvement
# ---------------------------------------------------------------------------
run_pytest D1 "agent-authored code is AST-gated: loading is not running" \
  packages/aleph-kernel/tests/test_ast_gate.py
run_pytest D2 "a skill is a kernel plugin, probed for usability" \
  packages/aleph-kernel/tests/test_skills.py
run_shell D3 "an agent authors a skill end to end" \
  "uv run pytest packages/aleph-kernel/tests/test_skills.py -q -p no:randomly \
     -k 'agent' 2>&1 | tail -1"
run_pytest D6 "the ported literature-review skill loads and works" \
  packages/aleph-kernel/tests/test_ported_skills.py
run_pytest D4 "spawn ledger: lineage, depth, fan-out and budget brakes" \
  packages/aleph-kernel/tests/test_spawn_ledger.py
run_pytest D5 "probation: a capability that degrades is retired automatically" \
  packages/aleph-kernel/tests/test_probation.py

# ---------------------------------------------------------------------------
# E — Deletion
# ---------------------------------------------------------------------------
run_shell E4 "workspace package count does not grow unchecked" \
  "n=\$(ls packages | wc -l); echo \"\$n packages\"; [ \"\$n\" -le 21 ]"
# Deliberately red: the patch contract shipped before its consumer, which is
# the exact defect class this refactor exists to remove. It goes green when the
# Claim Spine (part C) uses it, or the code gets deleted.
run_expected_red_shell E5 "aleph-belief patch contract still has no consumer" \
  "n=\$(grep -rl 'aleph_belief' --include='*.py' apps packages 2>/dev/null | grep -v 'packages/aleph-belief' | wc -l); [ \"\$n\" -ge 1 ]"
# E1, E2 and E3 are GONE, not skipped.
#
# They asked when the wiki could be deleted. `docs/decisions.md` D1 (2026-08-21)
# reverses that decision: the wiki and the RAG over the raw collection are two
# knowledge plugins and both stay. There is no deletion to unblock, so there is
# no condition to wait on — and a row that skips forever on a condition nobody
# intends to meet is indistinguishable from work that is merely late. That is
# the shape this whole gate exists to remove, so leaving them as permanent
# skips would have been the gate doing the thing it forbids.
#
# What survived from E is measurement, not deletion: whether claim-level
# retrieval beats chunk-level on the same eval (`docs/plan.md` WS-RS10). That is
# a number to report, not a gate on a removal.

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
# H — Model gateway
# ---------------------------------------------------------------------------
# H1 calls the gateway, so it needs one. An unreachable gateway is a SKIP and
# not a FAIL: CI has no model endpoint by design ("Aleph serves no models"), and
# a red row there would train everyone to ignore the colour.
gateway_up() {
  [ -n "${LITELLM_BASE_URL:-}" ] || return 1
  curl -sf --max-time 5 "${LITELLM_BASE_URL%/}/v1/models" \
    -H "Authorization: Bearer ${INSIGHTS_LITELLM_API_KEY:-}" >/dev/null 2>&1
}

if [ $NEEDS_SERVICES -eq 1 ] && gateway_up; then
  run_shell H1 "every bound model is served, and the embedder emits the column's dim" \
    "uv run python scripts/_acceptance/gateway_serves_bound_models.py"
elif [ $NEEDS_SERVICES -eq 1 ]; then
  skip H1 "no gateway reachable at ${LITELLM_BASE_URL:-<unset>}"
else
  skip H1 "needs postgres + a reachable gateway"
fi

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
    MISS) colour='\033[31m' ;;
    *)    colour='\033[90m' ;;
  esac
  printf "%-5s ${colour}%-6s\033[0m %s\n" "$id" "$status" "${detail:0:90}"
done

echo
bold "pass=$PASS  fail=$FAIL  red=$RED (known defects under test)  skip=$SKIP (not runnable here)  missing=$MISSING (subject gone)"
echo

if [ $SELF_CHECK -eq 1 ]; then
  bold "self-check — can these checks fail?"
  bash "$ROOT/scripts/_acceptance/self_check.sh" || FAIL=$((FAIL+1))
  echo
fi

if [ $MISSING -gt 0 ]; then
  echo "MISSING — $MISSING subject(s) this gate names do not exist. A check whose"
  echo "subject is gone is not a check that skipped; it is a check that cannot run"
  echo "and has been reporting nothing. Restore the subject or delete the check."
  exit 1
fi
if [ $FAIL -gt 0 ]; then
  echo "FAIL — $FAIL check(s) that should pass did not."
  exit 1
fi
if [ $STRICT -eq 1 ] && [ $SKIP -gt 0 ]; then
  echo "STRICT — $SKIP part(s) skipped, and --strict says a skip is a failure."
  echo "This runs where services are guaranteed, so 'cannot run here' is not true here."
  exit 1
fi
if [ "$MAX_SKIP" -ge 0 ] && [ $SKIP -gt "$MAX_SKIP" ]; then
  echo "SKIP BUDGET — $SKIP skipped, budget is $MAX_SKIP."
  echo "The budget only ratchets down. Make the check runnable, or delete it."
  exit 1
fi
if [ $SKIP -gt 0 ]; then
  echo "INCOMPLETE — $SKIP part(s) not runnable here. Nothing regressed."
  exit 0
fi
echo "COMPLETE — every part built and checked."
exit 0
