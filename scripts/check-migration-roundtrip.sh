#!/usr/bin/env bash
#
# Every migration's downgrade must actually run.
#
# There are 26 revisions under `apps/api/alembic/versions/` and every one has a
# `downgrade()` body — several substantial. Not one had ever been executed:
# `grep -rn downgrade .github/ scripts/ tests/` returned nothing. CI enforces
# `alembic check` for FORWARD drift, so the forward path is genuinely guarded and
# the reverse path was entirely unguarded — 26 functions written correctly and
# read by nothing, which is the defect class CLAUDE.md names as dominant.
#
# It runs against a SCRATCH database, created and dropped here. Downgrading the
# development database would destroy the corpus, and a check nobody dares run is
# not a check.
#
# Two modes:
#   --last        upgrade head, downgrade -1, upgrade head, alembic check (fast;
#                 this is what CI runs on every push)
#   --full        walk all the way down to base and back up again (slow; run it
#                 when a revision is added or when you want the whole chain)
#
# Requires a reachable Postgres superuser connection — it creates a database.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="--last"
[ "${1:-}" = "--full" ] && MODE="--full"

BASE_URL="${DATABASE_URL:-${ALEPH_TEST_DATABASE_URL:-}}"
if [ -z "$BASE_URL" ]; then
  echo "DATABASE_URL is required — this needs a Postgres it can create a database on" >&2
  exit 2
fi

SCRATCH="aleph_migration_roundtrip_$$"
# Swap the database name on the URL, leaving credentials and host alone.
SCRATCH_URL="$(python3 - "$BASE_URL" "$SCRATCH" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

url, name = sys.argv[1], sys.argv[2]
parts = urlsplit(url)
print(urlunsplit(parts._replace(path=f"/{name}")))
PY
)"

# psql needs a libpq URL, not SQLAlchemy's `+asyncpg` dialect form.
libpq() { printf '%s' "$1" | sed -E 's#\+[a-z]+://#://#'; }
ADMIN_LIBPQ="$(libpq "$BASE_URL")"

cleanup() {
  psql "$ADMIN_LIBPQ" -q -c "DROP DATABASE IF EXISTS \"$SCRATCH\" WITH (FORCE)" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "creating scratch database $SCRATCH"
psql "$ADMIN_LIBPQ" -q -c "DROP DATABASE IF EXISTS \"$SCRATCH\" WITH (FORCE)" >/dev/null 2>&1 || true
psql "$ADMIN_LIBPQ" -q -c "CREATE DATABASE \"$SCRATCH\""

# pgvector is created by the initial migration, but the extension has to be
# available to the cluster. If it is not, say so rather than failing inside
# alembic with a message about a missing type.
if ! psql "$(libpq "$SCRATCH_URL")" -tAc \
      "select 1 from pg_available_extensions where name='vector'" | grep -q 1; then
  echo "the 'vector' extension is not available on this cluster — cannot run the round trip" >&2
  exit 2
fi

export DATABASE_URL="$SCRATCH_URL"
cd apps/api

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

step "upgrade head"
uv run --quiet alembic upgrade head

if [ "$MODE" = "--full" ]; then
  step "downgrade base — every revision's downgrade, in order"
  uv run --quiet alembic downgrade base
  step "upgrade head again"
  uv run --quiet alembic upgrade head
else
  step "downgrade -1 (the newest revision's downgrade)"
  uv run --quiet alembic downgrade -1
  step "upgrade head again"
  uv run --quiet alembic upgrade head
fi

step "alembic check — the models and the schema still agree after the round trip"
uv run --quiet alembic check

printf '\n\033[32mOK\033[0m: the round trip ran and left no model drift\n'
