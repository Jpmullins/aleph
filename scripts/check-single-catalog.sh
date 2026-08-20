#!/usr/bin/env bash
#
# Two catalogs claiming one identity render differently and neither one errors.
#
# `A2UISurfaceView.tsx` and `SurfaceStreamProvider.tsx` each declared
# `ALEPH_V09_CATALOG_ID = "aleph://v1"` and each built its own `new Catalog(...)`
# under that id. They disagreed about exactly one argument: the pane copy passed
# every basic-catalog function, the other passed `[]`. `lib/copilot.tsx` built
# the chat renderer from the empty one, so a surface using `formatDate`,
# `equals` or `openUrl` rendered correctly in a pane and threw
# `Function not found in catalog 'aleph://v1'` in chat — same surface, same id,
# two behaviours, no build error and no test to catch it.
#
# This is the third time this repo has paid for duplicated catalog state; the
# previous round was three hand-maintained copies disagreeing about
# `ClaimCard.confidence`. The structural fix is one declaration and one builder
# in `aleph-catalog-v09.tsx`; this sweep is what stops a second one reappearing.
#
# Static — no build, no browser, no server.
#
# CI-wired. Fails on: a second catalog id declaration, a second `new Catalog(`,
# or the canonical builder dropping the basic-catalog functions.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SRC="apps/web/src"
CANON="$SRC/a2ui/aleph-catalog-v09.tsx"
fail=0

note() { printf '  %s\n' "$*" >&2; }

# 1 — exactly one declaration of the catalog id.
ids="$(grep -rn --include='*.ts' --include='*.tsx' -E '^\s*export const ALEPH_V09_CATALOG_ID\s*=' "$SRC" || true)"
n_ids="$(printf '%s' "$ids" | grep -c . || true)"
if [ "$n_ids" -ne 1 ]; then
  echo "FAIL: expected exactly 1 declaration of ALEPH_V09_CATALOG_ID, found $n_ids" >&2
  printf '%s\n' "$ids" | while IFS= read -r l; do [ -n "$l" ] && note "$l"; done
  fail=1
elif ! printf '%s' "$ids" | grep -q "^$CANON:"; then
  echo "FAIL: ALEPH_V09_CATALOG_ID is declared outside the canonical module" >&2
  note "expected it in $CANON"
  note "found: $ids"
  fail=1
fi

# 2 — exactly one construction, in the canonical module.
# `strip_comments` drops doc-comment and `//` lines so prose about the rule —
# including this file's own header — never counts as a violation.
strip_comments() { grep -vE '^[^:]+:[0-9]+:[[:space:]]*(\*|//|/\*)' || true; }

ctors="$(grep -rn --include='*.ts' --include='*.tsx' -E '(^|[^.[:alnum:]_])new Catalog\(' "$SRC" | strip_comments || true)"
n_ctors="$(printf '%s' "$ctors" | grep -c . || true)"
if [ "$n_ctors" -ne 1 ]; then
  echo "FAIL: expected exactly 1 'new Catalog(' under $SRC, found $n_ctors" >&2
  printf '%s\n' "$ctors" | while IFS= read -r l; do [ -n "$l" ] && note "$l"; done
  note "every renderer must call buildAlephCatalog() from aleph-catalog-v09.tsx"
  fail=1
elif ! printf '%s' "$ctors" | grep -q "^$CANON:"; then
  echo "FAIL: 'new Catalog(' appears outside the canonical module" >&2
  note "found: $ctors"
  fail=1
fi

# 3 — the canonical builder still carries the basic-catalog FUNCTIONS.
#     This is the argument the two copies disagreed about; dropping it silently
#     breaks every surface that binds a function, and only in some renderers.
if ! grep -q 'basicCatalog\.functions\.values()' "$CANON"; then
  echo "FAIL: buildAlephCatalog no longer passes basicCatalog.functions" >&2
  note "surfaces binding formatDate/equals/openUrl will throw at render time"
  note "see $CANON"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "one catalog id, one constructor, functions present"
