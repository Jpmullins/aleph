#!/usr/bin/env bash
#
# WP-4 sub-spec (a) — no-self-fetch enforcement.
#
# The right panel is declarative + data-bound: every canonical surface renders
# ONLY from the data model the backend `*_v09` builders stream (createSurface +
# updateComponents + updateDataModel deltas). A component that fetches or polls
# its own data has smuggled a second, un-ordered data path around the surface
# stream — the exact anti-pattern this package removes. Mutations go through the
# ledger-audited action router via `onAction(name, params)`, never react-query.
#
# This grep fails CI on any of these inside apps/web/src/a2ui/components:
#   useQuery | useMutation | refetchInterval | EventSource | fetch( |
#   api.get | api.post | api.put | api.delete
#
# ALLOWLIST — EMPTY as of WP-4e close (Final State item 1). Every catalog card
# and surface renders exclusively from bound props; the grep over
# a2ui/components is clean with NO exceptions. The historically-allowlisted
# cards are all rebuilt:
#   - ChartCard.tsx   DONE(WP-4c): inline Vega-Lite spec / artifact URI
#   - TableCard.tsx   DONE(WP-4e): bound rows/columns in props
#   - SourceCard.tsx  DONE(WP-4e): Library builder supplies normalized_preview
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/apps/web/src/a2ui/components"

# Matches real self-fetch CODE (calls / generics / imports), not prose that
# merely names these APIs in a comment. A component that imports react-query is
# self-fetching by definition, so the import itself is a hit.
PATTERN='@tanstack/react-query|useQuery[[:space:]]*[(<]|useMutation[[:space:]]*[(<]|refetchInterval|EventSource[[:space:]]*\(|[^A-Za-z]fetch[[:space:]]*\(|api\.(get|post|put|delete)'
ALLOWLIST=()

is_allowed() {
  local base="$1"
  for a in ${ALLOWLIST[@]+"${ALLOWLIST[@]}"}; do
    [ "$base" = "$a" ] && return 0
  done
  return 1
}

fail=0
# `grep -rlE` lists files with at least one hit; iterate and skip the allowlist.
while IFS= read -r file; do
  [ -z "$file" ] && continue
  base="$(basename "$file")"
  if is_allowed "$base"; then
    continue
  fi
  fail=1
  echo "✗ self-fetch in $file:"
  grep -nE "$PATTERN" "$file" | sed 's/^/    /'
done < <(grep -rlE "$PATTERN" "$DIR" --include='*.tsx' --include='*.ts' 2>/dev/null || true)

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "Components must render only from bound props; route mutations through onAction()."
  exit 1
fi

echo "✓ no self-fetch in a2ui/components (allowlist: ${ALLOWLIST[*]:-<empty>})"
