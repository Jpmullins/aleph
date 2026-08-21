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
mutate() { # mutate FILE PERL_EXPR
  local f="$1" expr="$2"
  cp "$f" "$BACKUP_DIR/$(echo "$f" | tr / _)"
  MUTATED+=("$f")
  perl -0pi -e "$expr" "$f"
}

unmutate() { # unmutate FILE
  local f="$1"
  cp "$BACKUP_DIR/$(echo "$f" | tr / _)" "$f"
}

OK=0; BAD=0

# probe NAME FILE SED_EXPR CHECK_CMD
probe() {
  local name="$1" file="$2" expr="$3" cmd="$4"
  mutate "$file" "$expr"
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
  's/titles, summaries and aliases only/something or other/' \
  "uv run python -c \"
from aleph_assistant.retrieval import router as r
assert 'titles' in r._MISS_REASON and 'bodies' in r._MISS_REASON
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

probe "check-acceptance-claims notices a cited test that does not exist" \
  docs/acceptance.md \
  's/packages\/aleph-core\/tests\/test_rrf.py/packages\/aleph-core\/tests\/test_no_such_thing.py/' \
  "./scripts/check-acceptance-claims.sh"

# The mutation that matters here is not "delete the rule" — it is "add an allow
# ahead of the deny", which is what WS-H1 will legitimately want to do and is
# the way this gate silently reopens.
probe "check-agent-fs-permissions notices an allow ahead of the deny" \
  apps/api/src/aleph_api/copilot_agent.py \
  's/        permissions=\[\n            FilesystemPermission/        permissions=[\n            FilesystemPermission(operations=["write"], paths=["\/skills\/**"], mode="allow"),\n            FilesystemPermission/' \
  "./scripts/check-agent-fs-permissions.sh"

# The subagent half is the one that goes wrong silently: a spec declaring its
# own middleware OVERRIDES the parent's guard rather than adding to it.
probe "check-agent-middleware notices one unguarded subagent" \
  apps/api/src/aleph_api/subagents/analyst.py \
  's/\n\s+"middleware": \[AlephAgentMiddleware\(\)\],//' \
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
