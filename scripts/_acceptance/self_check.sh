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

mutate() { # mutate FILE SED_EXPR
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

# Service-backed: only meaningful when postgres is up.
if (echo > /dev/tcp/localhost/5432) >/dev/null 2>&1; then
  export ALEPH_DATABASE_URL="${ALEPH_TEST_DATABASE_URL:-postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph}"
  export DATABASE_URL="$ALEPH_DATABASE_URL" ALEPH_AUTH_MODE=local
  export REDIS_URL="${ALEPH_TEST_REDIS_URL:-redis://localhost:6379/0}"
  probe "stale-link expansion is really filtered" \
    packages/aleph-assistant/src/aleph_assistant/retrieval/router.py \
    's/                    WikiLink\.src_revision_id == WikiPage\.current_revision_id,\n//' \
    "uv run pytest tests/e2e/test_retrieval_finds_body_text.py::test_expansion_ignores_links_from_superseded_revisions -q -p no:randomly"
else
  printf '  \033[90m%-8s\033[0m stale-link mutation (needs postgres)\n' "skip"
fi

# H1's subject is a database row, not a source file, so it needs its own
# mutation rather than `probe`. Binds a model no gateway serves, asserts the
# check notices, and restores in a finally so a failure here cannot leave the
# profile table pointing at a ghost.
if (echo > /dev/tcp/localhost/5432) >/dev/null 2>&1 \
   && curl -sf --max-time 5 "${LITELLM_BASE_URL:-http://localhost:8010}/v1/models" >/dev/null 2>&1; then
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
