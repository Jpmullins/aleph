#!/usr/bin/env bash
#
# Can these checks fail?
#
# For each mutation below: break the subject, run its check, and require the
# check to NOTICE. A check that stays green while its subject is broken is worse
# than no check — it converts an unexamined assumption into a green light, which
# is precisely how this repo shipped a 31/32 scorecard over a central hypothesis
# that was never true.
#
# Every mutation is applied to a backup-restored copy and reverted in a trap, so
# an interrupt cannot leave the tree mutated. The script verifies the tree is
# clean on exit.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BACKUP_DIR="$(mktemp -d)"
declare -a MUTATED=()

restore_all() {
  for f in "${MUTATED[@]}"; do
    [ -f "$BACKUP_DIR/$(echo "$f" | tr / _)" ] && cp "$BACKUP_DIR/$(echo "$f" | tr / _)" "$f"
  done
  rm -rf "$BACKUP_DIR"
}
trap restore_all EXIT INT TERM

# NOTE on the expressions below: perl runs with -0 (whole file slurped), so an
# expression anchored with `^` needs the /m modifier or it matches only the very
# start of the file — and a mutation that silently fails to apply is reported as
# "this check cannot fail", which sends you to fix a check that is fine.
#: Set by `mutate` when the substitution changed nothing. A no-op mutation is a
#: HARD failure, not a quiet pass — see `mutate`.
MUTATION_WAS_NOOP=0

mutate() { # mutate FILE PERL_EXPR
  local f="$1" expr="$2" backup
  backup="$BACKUP_DIR/$(echo "$f" | tr / _)"
  cp "$f" "$backup"
  MUTATED+=("$f")
  perl -0pi -e "$expr" "$f"

  # A mutation that changes nothing proves nothing — and it reports "can fail".
  #
  # This has happened three times in this repository and every instance looked
  # fine in review:
  #   * a `^` anchor under `perl -0` (slurp mode) matching only the file's first
  #     line, so two probes silently patched nothing;
  #   * a probe targeting the `permissions=[` literal after WS-H1 moved those
  #     rules into a function, so the pattern matched no text at all;
  #   * a probe naming a specific migration file that a newer migration had
  #     displaced, so the check it drove no longer executed that file.
  #
  # In each case the probe printed "can fail" having broken nothing, which is
  # strictly worse than no probe: it occupies the slot where a real one would
  # go, and it makes the self-check's own count of caught mutations a lie.
  #
  # The green-first rule below catches a check that is already red. This catches
  # the other half — a mutation that never happened.
  if cmp -s "$f" "$backup"; then
    MUTATION_WAS_NOOP=1
  else
    MUTATION_WAS_NOOP=0
  fi
}

unmutate() { # unmutate FILE
  local f="$1"
  cp "$BACKUP_DIR/$(echo "$f" | tr / _)" "$f"
}

OK=0; BAD=0

# probe NAME FILE SED_EXPR CHECK_CMD
#
# A mutation test has TWO halves and this only ever ran one of them. It applied
# the mutation and asked whether the check went red — so a check that was
# ALREADY red reported "can fail" no matter what the mutation did, or whether it
# applied at all. Two of these were silent no-ops for exactly that reason
# (`perl -0` needs /m for a `^` anchor), and one covered a sweep that was red on
# the tree, which made its probe a tautology.
#
# So: require GREEN first, then require RED under mutation. A check that is
# already failing is reported as such and counted as a failure of the
# self-check, because "I cannot tell you whether this check works" is not a
# pass.
probe() {
  local name="$1" file="$2" expr="$3" cmd="$4"

  if ! bash -c "$cmd" >/dev/null 2>&1; then
    printf '  \033[31m%-8s\033[0m %s — the check is ALREADY red, so this probe proves nothing\n' \
      "UNTESTED" "$name"
    BAD=$((BAD+1))
    return
  fi

  mutate "$file" "$expr"
  if [ "$MUTATION_WAS_NOOP" -eq 1 ]; then
    printf '  \033[31m%-8s\033[0m %s — the mutation changed NOTHING, so this proves nothing\n' \
      "NO-OP" "$name"
    BAD=$((BAD+1))
    unmutate "$file"
    return
  fi
  if bash -c "$cmd" >/dev/null 2>&1; then
    printf '  \033[31m%-8s\033[0m %s — check stayed GREEN while broken\n' "CANNOT" "$name"
    BAD=$((BAD+1))
  else
    printf '  \033[32m%-8s\033[0m %s\n' "can fail" "$name"
    OK=$((OK+1))
  fi
  unmutate "$file"
}

KERNEL_TESTS="uv run pytest packages/aleph-kernel/tests -q -p no:randomly"

probe "blast radius actually reads the graph" \
  packages/aleph-kernel/src/aleph_kernel/support.py \
  's/collateral = \(before - after\) - \{target\}/collateral = frozenset()/' \
  "$KERNEL_TESTS"

probe "the probe gate actually gates" \
  packages/aleph-kernel/src/aleph_kernel/kernel.py \
  's/if not result\.passed:/if False:/' \
  "$KERNEL_TESTS"

probe "unwind is LIFO" \
  packages/aleph-kernel/src/aleph_kernel/effects.py \
  's/inverse = self\._inverses\.pop\(\)/inverse = self._inverses.pop(0)/' \
  "$KERNEL_TESTS"

probe "undeclared access is refused" \
  packages/aleph-kernel/src/aleph_kernel/context.py \
  's/if key not in self\._requires:/if False:/' \
  "$KERNEL_TESTS"

probe "a failed setup still unwinds" \
  packages/aleph-kernel/src/aleph_kernel/kernel.py \
  's/                await scope\.unwind\(\)\n                mounted\.state = State\.FAILED\n                mounted\.failure = f"setup raised/                mounted.state = State.FAILED\n                mounted.failure = f"setup raised/' \
  "$KERNEL_TESTS"

probe "the honest-miss diagnostic is required" \
  packages/aleph-assistant/src/aleph_assistant/retrieval/router.py \
  's/titles, summaries, aliases and page bodies/something or other/' \
  "uv run python -c \"
from aleph_assistant.retrieval import router as r
assert 'bodies' in r._MISS_REASON and 'source' in r._MISS_REASON
\""

# ---------------------------------------------------------------------------
# The sweeps. Five of these run in CI on every push and none had ever been
# observed failing — `scripts/acceptance.sh --self-check` covered the kernel and
# the retrieval router and nothing else, so "six probes, all green" said nothing
# about the six gates that actually block a merge.
# ---------------------------------------------------------------------------

probe "check-catalog-generated notices a hand-edited generated file" \
  apps/web/src/a2ui/catalog.ts \
  's/export const CATALOG_VERSION = "1.0.0";/export const CATALOG_VERSION = "9.9.9";/' \
  "./scripts/check-catalog-generated.sh"

probe "check-single-catalog notices a second catalog identity" \
  apps/web/src/lib/workspace-ui.tsx \
  's/^export interface PaneKindDef \{/export const ALEPH_V09_CATALOG_ID = "aleph:\/\/v1";\n\nexport interface PaneKindDef {/m' \
  "./scripts/check-single-catalog.sh"

probe "check-surface-bindings notices a prop the client never declares" \
  packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py \
  's/"pages": \{"path": "\/pages"\},/"pages": {"path": "\/pages"}, "not_a_declared_prop": {"path": "\/nope"},/' \
  "./scripts/check-surface-bindings.sh"

probe "check-graph-state-keys notices an undeclared state write" \
  packages/aleph-research/src/aleph_research/research_workflow.py \
  's/        return \{"candidates": candidates, "seen_keys": sorted\(seen\)\}/        return {"candidates": candidates, "seen_keys": sorted(seen), "a_key_no_typed_dict_declares": 1}/' \
  "./scripts/check-graph-state-keys.sh"

probe "check-dead-refs notices a path that is not there" \
  docs/operations.md \
  's/^# Operations/# Operations\n\nSee `deploy\/definitely-not-a-real-directory\/README.md`./' \
  "./scripts/check-dead-refs.sh"

# An undocumented sweep, which is how the inventory in operations.md drifted from
# two to twenty-one without a single gate noticing.
# An evidence path naming a file that is gone. `audit/claims.yaml` is where an
# auditor is pointed, so a dead path there is the strongest form of the defect
# this sweep exists for — and it was out of scope until 2026-08-22, when adding
# it found ELEVEN.
#
# The bogus path is ASSEMBLED at runtime and never spelled here. This file is
# itself in the sweep's PROSE list, so writing the literal made self_check.sh
# carry a dead reference of its own and the check went red before the mutation
# — reported honestly as UNTESTED by the already-red guard rather than as a
# pass, which is the only reason it was noticed.
CLAIMS_SUBJECT="apps/web/src/components/Rail.tsx"
# Derived from the subject rather than written out. Interpolating a suffix
# onto a literal is not enough: the sweep's token pattern stops at the `$`, so
# the prefix it leaves behind is itself a path that does not resolve — and so
# is any copy of it in a comment explaining the problem.
CLAIMS_BOGUS="${CLAIMS_SUBJECT%.tsx}IsGone.tsx"
if grep -q -- "$CLAIMS_SUBJECT" audit/claims.yaml 2>/dev/null; then
  cp audit/claims.yaml "$BACKUP_DIR/claims.bak"
  perl -pi -e "s#\Q$CLAIMS_SUBJECT\E#$CLAIMS_BOGUS#" audit/claims.yaml
  if cmp -s audit/claims.yaml "$BACKUP_DIR/claims.bak"; then
    printf '  \033[31m%-8s\033[0m %s — the mutation changed NOTHING\n' \
      "NO-OP" "an audit claim whose evidence is gone is noticed"
    BAD=$((BAD+1))
  elif ./scripts/check-dead-refs.sh >/dev/null 2>&1; then
    printf '  \033[31m%-8s\033[0m %s — check stayed GREEN while broken\n' \
      "CANNOT" "an audit claim whose evidence is gone is noticed"
    BAD=$((BAD+1))
  else
    printf '  \033[32m%-8s\033[0m %s\n' "can fail" "an audit claim whose evidence is gone is noticed"
    OK=$((OK+1))
  fi
  cp "$BACKUP_DIR/claims.bak" audit/claims.yaml
fi

probe "check-dead-refs notices a sweep the operations doc stopped naming" \
  docs/operations.md \
  's/`check-page-lock\.sh`/`check-page-lock-renamed-so-the-doc-no-longer-names-it.sh`/' \
  "./scripts/check-dead-refs.sh"

probe "check-acceptance-claims notices a cited test that does not exist" \
  docs/acceptance.md \
  's/packages\/aleph-core\/tests\/test_rrf.py/packages\/aleph-core\/tests\/test_no_such_thing.py/' \
  "./scripts/check-acceptance-claims.sh"

# The two citation shapes that run as an ERROR rather than as a pass or a fail.
# Both were live in docs/plan.md WS-D2 and both survived the sweep above: the
# first because the plan is allowed to name a file that does not exist yet, the
# second because a node id with an empty path part was never tokenized at all.
# A criterion written either way is not unmet, it is unmeasurable, and a reader
# scanning for red sees neither.
probe "check-acceptance-claims notices a real test cited under the wrong directory" \
  docs/plan.md \
  's|apps/api/tests/unit/test_agent_cost_callback.py::test_no_usage_writes_an_unpriced_row|tests/unit/test_agent_cost_callback.py::test_no_usage_writes_an_unpriced_row|' \
  "./scripts/check-acceptance-claims.sh"

probe "check-acceptance-claims notices a bare ::test_id no file defines" \
  docs/plan.md \
  's|apps/api/tests/unit/test_agent_cost_callback.py::test_no_usage_writes_an_unpriced_row_rather_than_nothing|::test_no_usage_writes_unknown_row|' \
  "./scripts/check-acceptance-claims.sh"

# The scoreboard's two missing edges. acceptance.sh ran 64 checks while
# docs/acceptance.md listed 46 — the plugin cluster A7-A11, which CLAUDE.md
# calls the product, was on no scoreboard at all, and CLAUDE.md cited two rows
# (C10, F7) that existed nowhere. A row id is not a path, so check-dead-refs
# could not see either.
probe "check-acceptance-rows notices a gate row on no scoreboard" \
  docs/acceptance.md \
  's/^\| B1e \|/| B1x |/' \
  "./scripts/check-acceptance-rows.sh"

probe "check-acceptance-rows notices prose citing a row that does not exist" \
  CLAUDE.md \
  's/acceptance A8\./acceptance A99./' \
  "./scripts/check-acceptance-rows.sh"

# The mutation that matters is not "delete the rule" — it is "widen the allow",
# which is how this gate silently reopens.
#
# WS-H1 has now legitimately added an allow ahead of the deny, for exactly one
# nested prefix. So the dangerous version is no longer "an allow exists"; it is
# an allow on the WHOLE of `/skills/**`, which reopens every bundled SKILL.md
# while still looking like the narrow rule.
#
# The previous mutation here targeted the inline `permissions=[` literal, and
# WS-H1 moved the rules into a function — so it matched nothing and the probe
# passed without ever breaking anything. A no-op mutation reports "can fail"
# and proves precisely nothing, which is the failure this whole file exists to
# prevent, occurring inside it.
probe "check-agent-fs-permissions notices an allow widened to all of /skills" \
  apps/api/src/aleph_api/copilot_agent.py \
  's/paths=\[f"\{AUTHORED_PREFIX\}\*\*"\], mode="allow"/paths=["\/skills\/**"], mode="allow"/' \
  "./scripts/check-agent-fs-permissions.sh"

# The mutation that defeated the FIRST version of this sweep: `with_for_update()`
# → `with_for_update(read=True)`. FOR SHARE is not a lock for `max + 1`, and the
# sweep matched the method name without looking at the mode, then printed "all
# FOR UPDATE" — which was false.
probe "check-page-lock notices a shared lock masquerading as an exclusive one" \
  packages/aleph-wiki/src/aleph_wiki/wiki_service.py \
  's/\.with_for_update\(\)/.with_for_update(read=True)/g' \
  "./scripts/check-page-lock.sh"

# The subagent half is the one that goes wrong silently: a spec declaring its
# own middleware OVERRIDES the parent's guard rather than adding to it.
probe "check-agent-middleware notices one unguarded subagent" \
  apps/api/src/aleph_api/subagents/analyst.py \
  's/\n[^\n]*"middleware": \[AlephAgentMiddleware[^\n]*//' \
  "./scripts/check-agent-middleware.sh"

# check-pane-registry's subject is the CLIENT growing a second copy of the
# server's pane list, so the mutation belongs in the client.
probe "check-pane-registry notices a hardcoded pane list in the client" \
  apps/web/src/lib/workspace-ui.tsx \
  's/^export interface PaneKindDef \{/const HARDCODED_PANES = ["wiki", "library", "notes", "hypotheses"];\n\nexport interface PaneKindDef {/m' \
  "./scripts/check-pane-registry.sh"

# ---------------------------------------------------------------------------
# Retrieval, after WS-RS1. Each of these mutations recreates a specific half of
# the outage that left `document_chunks` empty against 75 ingested sources.
# ---------------------------------------------------------------------------

# The assistant must say when half its search did not run. Returning `[]` on a
# dead embedder is the same answer as "this project knows nothing about that".
probe "a dead embedder is reported, not swallowed" \
  packages/aleph-assistant/src/aleph_assistant/retrieval/router.py \
  's/            self\._degraded = "embedder_unavailable"/            pass/' \
  "uv run pytest packages/aleph-assistant/tests/test_router_degradation.py -q -p no:randomly"

# Service-backed: only meaningful when postgres is up.
# The port comes from the URL the tests will use. Probing 5432 unconditionally
# meant the dev stack (Postgres on 5442) silently skipped every DB-backed probe.
_DB_URL="${ALEPH_TEST_DATABASE_URL:-postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph}"
_HOSTPORT="${_DB_URL#*://}"; _HOSTPORT="${_HOSTPORT#*@}"; _HOSTPORT="${_HOSTPORT%%/*}"
_PG_HOST="${_HOSTPORT%%:*}"; _PG_PORT="${_HOSTPORT##*:}"
[ "$_PG_PORT" = "$_PG_HOST" ] && _PG_PORT=5432
if (echo > "/dev/tcp/$_PG_HOST/$_PG_PORT") >/dev/null 2>&1; then
  export ALEPH_DATABASE_URL="$_DB_URL"
  export DATABASE_URL="$ALEPH_DATABASE_URL" ALEPH_AUTH_MODE=local
  export REDIS_URL="${ALEPH_TEST_REDIS_URL:-redis://localhost:6379/0}"
  probe "stale-link expansion is really filtered" \
    packages/aleph-assistant/src/aleph_assistant/retrieval/router.py \
    's/                    WikiLink\.src_revision_id == WikiPage\.current_revision_id,\n//' \
    "uv run pytest tests/e2e/test_retrieval_finds_body_text.py::test_expansion_ignores_links_from_superseded_revisions -q -p no:randomly"

  # The chunk write must be COMMITTED before the embed call. Sharing one
  # transaction rolls the chunks back with the failed embed, which is exactly
  # the shape that emptied the index.
  probe "chunks are committed before the embedder is called" \
    packages/aleph-rks/src/aleph_rks/indexing.py \
    's/            src\.status = "indexed"\n        await session\.commit\(\)/            src.status = "indexed"\n        await session.rollback()/' \
    "uv run pytest tests/integration/test_chunk_embed_degrades.py::test_dead_embedder_still_writes_chunks -q -p no:randomly"

  # 26 downgrades had never been executed. This is the slowest probe here (it
  # creates a scratch database) and it is worth it: the forward path was guarded
  # by `alembic check` and the reverse path by nothing at all.
  #
  # The revision is resolved at RUN TIME, not written here. This probe used to
  # name `20260821_2330_rs1_chunks_before_embeddings.py`, and
  # `check-migration-roundtrip.sh --last` downgrades only the NEWEST revision —
  # so the moment WS-P7 added a migration, the probe was mutating a file the
  # check no longer executed. It reported "can fail" while breaking nothing,
  # which is worse than not existing: it occupied the slot where a real probe
  # would have gone. Only the green-first rule caught it.
  HEAD_MIGRATION="$(ls apps/api/alembic/versions/*.py | sort | tail -1)"
  probe "the migration round trip notices a broken downgrade" \
    "$HEAD_MIGRATION" \
    's/def downgrade\(\) -> None:/def downgrade() -> None:\n    op.drop_table("projects")/' \
    "./scripts/check-migration-roundtrip.sh"

  # A run whose owner died must be failed, not left claiming to run.
  probe "the startup reaper actually fails a stale run" \
    packages/aleph-db/src/aleph_db/repos/agent_runs.py \
    's/        run\.status = "failed"/        pass/' \
    "uv run pytest tests/integration/test_agent_run_reaper.py::test_a_run_running_past_the_deadline_is_failed -q -p no:randomly"
else
  printf '  \033[90m%-8s\033[0m 3 database-backed mutations (needs postgres)\n' "skip"
fi

# H1's subject is a database row, not a source file, so it needs its own
# mutation rather than `probe`. Binds a model no gateway serves, asserts the
# check notices, and restores in a finally so a failure here cannot leave the
# profile table pointing at a ghost.
if (echo > /dev/tcp/localhost/5432) >/dev/null 2>&1 \
   && [ -n "${LITELLM_BASE_URL:-}" ] \
   && curl -sf --max-time 5 "${LITELLM_BASE_URL%/}/v1/models" \
        -H "Authorization: Bearer ${INSIGHTS_LITELLM_API_KEY:-}" >/dev/null 2>&1; then
  H1_OUT=$(uv run python - <<'PYEOF' 2>/dev/null
import asyncio, os, subprocess, sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

URL = os.environ.get("ALEPH_DATABASE_URL", "")
GHOST = "self-check-ghost-model"

async def main() -> str:
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "select id, bindings_jsonb->'synthesis'->>'model' from model_profiles "
                "where bindings_jsonb ? 'synthesis' limit 1"))).first()
            if not row:
                return "SKIP"
            pid, original = row[0], row[1]
            await conn.execute(text(
                "update model_profiles set bindings_jsonb = "
                "jsonb_set(bindings_jsonb,'{synthesis,model}', to_jsonb(cast(:g as text))) where id = :i"),
                {"g": GHOST, "i": pid})
        try:
            rc = subprocess.run(
                [sys.executable, "scripts/_acceptance/gateway_serves_bound_models.py"],
                capture_output=True, timeout=300).returncode
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "update model_profiles set bindings_jsonb = "
                    "jsonb_set(bindings_jsonb,'{synthesis,model}', to_jsonb(cast(:o as text))) where id = :i"),
                    {"o": original, "i": pid})
        return "CAUGHT" if rc != 0 else "MISSED"
    finally:
        await engine.dispose()

print(asyncio.run(main()))
PYEOF
)
  case "$H1_OUT" in
    CAUGHT) printf '  \033[32m%-8s\033[0m %s\n' "can fail" "a bound model the gateway does not serve"
            OK=$((OK+1)) ;;
    MISSED) printf '  \033[31m%-8s\033[0m %s — check stayed GREEN while broken\n' "CANNOT" "a bound model the gateway does not serve"
            BAD=$((BAD+1)) ;;
    *)      printf '  \033[90m%-8s\033[0m gateway-binding mutation (no profile rows)\n' "skip" ;;
  esac
else
  printf '  \033[90m%-8s\033[0m gateway-binding mutation (needs postgres + gateway)\n' "skip"
fi

# ---------------------------------------------------------------------------
# The web sweeps and the bridge checks.
#
# All four were written correctly and wired into nothing — not acceptance, not
# CI, not here. That is the defect class CLAUDE.md names as dominant, and it is
# worse inside a sweep than in product code, because the sweep is what was
# supposed to catch it. Wiring them into the gate is half; proving each can go
# red is the half that makes the green mean something.
# ---------------------------------------------------------------------------

if [ -f apps/web/src/components/Rail.tsx ]; then
  # A module nothing imports. Created and removed here rather than mutated,
  # because unreachability is about the import GRAPH and no edit to an existing
  # file can produce it.
  cat > apps/web/src/components/__selfcheck_orphan.tsx <<'ORPHAN'
export const orphan = () => null;
ORPHAN
  if ./scripts/check-web-dead-code.sh >/dev/null 2>&1; then
    printf '  \033[31m%-8s\033[0m %s — check stayed GREEN while broken\n' \
      "CANNOT" "web dead-code sweep notices an unreachable module"
    BAD=$((BAD+1))
  else
    printf '  \033[32m%-8s\033[0m %s\n' "can fail" "web dead-code sweep notices an unreachable module"
    OK=$((OK+1))
  fi
  rm -f apps/web/src/components/__selfcheck_orphan.tsx
fi

if [ -f apps/web/src/styles.css ]; then
  # A hand-authored rule nothing wears. Appended at EOF rather than spliced in:
  # a class written between two `@import` statements sat in a chunk the sweep
  # skipped for containing an `@`, which was a hole in the sweep and not a
  # property of the probe — closed in check-web-dead-css.sh, and the EOF anchor
  # keeps this probe independent of that fix.
  probe "web dead-CSS sweep notices a class nothing applies" \
    apps/web/src/styles.css \
    's|\z|\n.selfcheck-zombie { color: red; }\n|' \
    "./scripts/check-web-dead-css.sh"
fi

if [ -f apps/web/src/components/Rail.tsx ]; then
  # One rounded corner. `--radius` is 0px and the ratchet pin is ZERO, so a
  # single added `rounded-lg` must move it. The plan's own mutation named
  # `border border-line bg-surface`, which does not occur in this file (it is
  # `border-r`), so perl edited nothing and the ratchet certified a mutation
  # that never happened.
  probe "design-token ratchet notices one added corner" \
    apps/web/src/components/Rail.tsx \
    's/border-r border-line bg-surface/border-r border-line rounded-lg bg-surface/' \
    "./scripts/check-web-drift.sh --ratchet"
fi

# The meta-check. A sweep nothing runs is the defect class this whole harness
# exists to catch, and it has landed three times — so the probe adds an unwired
# sweep and requires the check to notice.
if [ -f scripts/check-sweeps-are-wired.sh ]; then
  # The name is BUILT, never written literally.
  #
  # `check-sweeps-are-wired.sh` greps this very file for each sweep's basename,
  # so spelling the orphan's name here would make this file its consumer and
  # the probe would report the check green having proved nothing — a self-
  # defeating probe, which is the exact shape the NO-OP guard above exists for.
  ORPHAN="$(printf 'scripts/%s-%s-probe.sh' check selfcheck-orphan)"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$ORPHAN"
  chmod +x "$ORPHAN"
  if ./scripts/check-sweeps-are-wired.sh >/dev/null 2>&1; then
    printf '  \033[31m%-8s\033[0m %s — check stayed GREEN while broken\n' \
      "CANNOT" "an unwired sweep is noticed"
    BAD=$((BAD+1))
  else
    printf '  \033[32m%-8s\033[0m %s\n' "can fail" "an unwired sweep is noticed"
    OK=$((OK+1))
  fi
  rm -f "$ORPHAN"
fi

# The import sweep, mutated by UNTRACKING a module rather than by editing a
# file: the defect it exists for is invisible in the working tree, so a normal
# `probe` (which edits and restores content) could not express it. The file
# stays on disk throughout; only the index changes, and it is restored either
# way.
IMPORT_SUBJECT="apps/api/src/aleph_api/routes/background_tasks.py"
if git ls-files --error-unmatch "$IMPORT_SUBJECT" >/dev/null 2>&1; then
  git rm --cached -q "$IMPORT_SUBJECT"
  if ./scripts/check-imports-resolve.sh >/dev/null 2>&1; then
    printf '  \033[31m%-8s\033[0m %s — check stayed GREEN while broken\n' \
      "CANNOT" "an import of an untracked module is noticed"
    BAD=$((BAD+1))
  else
    printf '  \033[32m%-8s\033[0m %s\n' "can fail" "an import of an untracked module is noticed"
    OK=$((OK+1))
  fi
  git add -f "$IMPORT_SUBJECT"
else
  printf '  \033[33m%-8s\033[0m %s\n' "skip" "$IMPORT_SUBJECT is not tracked — cannot untrack it"
fi

# A security override for a package nothing depends on any more. Mutated by
# ADDING one rather than by removing a real one: the failure mode is an override
# outliving its reason, so the mutation has to be an override with no reason.
probe "an override for a package nothing depends on is noticed" \
  pnpm-workspace.yaml \
  's/^overrides:$/overrides:\n  a-package-nothing-here-depends-on: ">=9.9.9"/m' \
  "./scripts/check-security-overrides.sh"

# A modal that skips the trap. This is the mutation that left every gate green
# when WS-B1 shipped: two of its three modals had no test at all, so either
# could lose Escape, the focus trap and focus restore without anything noticing.
probe "a dialog outside Modal.tsx is noticed" \
  apps/web/src/components/ProjectList.tsx \
  's/<Modal title=/<div className="fixed inset-0" role="dialog" aria-modal="true" title=/' \
  "./scripts/check-modals-are-trapped.sh"

# A bound prop declared as a LITERAL. `GroundingSurface` and `InspectorSurface`
# both shipped this way — five props each typed `z3.any()`, so the binder passed
# `{path: "/runs"}` through verbatim and React unmounted the pane on every open,
# while the sweep printed "all declared client-side".
probe "a bound prop declared unresolvably is noticed" \
  apps/web/src/a2ui/aleph-catalog-v09.tsx \
  's/sections: CommonSchemas\.\w+/sections: z3.any()/' \
  "./scripts/check-surface-bindings.sh"

# The RUNNER itself. Every other probe here mutates a check's subject; this one
# mutates the thing that reports on all of them.
#
# `run_shell` ran each command in a child `bash -c`, and `pipefail` is a shell
# option rather than an exported variable — so for the 24 of 33 rows that end in
# `| tail -1` or `| head -2`, the recorded status was the pager's, which is
# always 0. Reproduced on the real A8 command against an unreachable database:
# `5 failed, 5 passed, 5 errors` and `rc=0`, recorded as PASS. A gate that
# cannot report a failure is not a gate, and this is the one the whole project
# reads as its scoreboard.
if grep -q 'set -o pipefail; set -e; \$cmd' scripts/acceptance.sh 2>/dev/null; then
  RUNNER_RC=$(bash -c 'out="$(bash -c "set -o pipefail; set -e; false | tail -1" 2>&1)"; echo $?')
  MASKED_RC=$(bash -c 'out="$(bash -c "false | tail -1" 2>&1)"; echo $?')
  if [ "$RUNNER_RC" = "1" ] && [ "$MASKED_RC" = "0" ]; then
    printf '  \033[32m%-8s\033[0m %s\n' "can fail" "a piped acceptance command reports its own status"
    OK=$((OK+1))
  else
    printf '  \033[31m%-8s\033[0m %s — guarded=%s unguarded=%s\n' \
      "CANNOT" "a piped acceptance command reports its own status" "$RUNNER_RC" "$MASKED_RC"
    BAD=$((BAD+1))
  fi
else
  printf '  \033[31m%-8s\033[0m %s\n' "CANNOT" \
    "acceptance.sh no longer sets pipefail in the child — 24 rows cannot fail"
  BAD=$((BAD+1))
fi

probe "the runtime bridge check notices an any-origin proxy" \
  apps/copilot-runtime/src/server.ts \
  's/^  cors: \{$/  cors: true, \/\/ probe\n  _unused: {/m' \
  "./scripts/check-runtime-bridge.sh"

probe "the runtime bridge check notices a credential-less browser" \
  apps/web/src/lib/copilot.tsx \
  's/^      headers=\{headers\}$/      \/\/ probe removed/m' \
  "./scripts/check-runtime-bridge.sh"

echo
if [ -n "$(git status --porcelain 2>/dev/null)" ] && [ ${#MUTATED[@]} -gt 0 ]; then
  # Restoration happens in the trap; warn only if a mutated file still differs.
  for f in "${MUTATED[@]}"; do
    if ! diff -q "$f" "$BACKUP_DIR/$(echo "$f" | tr / _)" >/dev/null 2>&1; then
      echo "WARNING: $f was not restored"
      BAD=$((BAD+1))
    fi
  done
fi

if [ $BAD -gt 0 ]; then
  echo "self-check FAILED — $BAD check(s) cannot fail. Fix the check, not the code."
  exit 1
fi
echo "self-check passed — all $OK mutations were caught."
exit 0
