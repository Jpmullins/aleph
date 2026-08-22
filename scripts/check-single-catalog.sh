#!/usr/bin/env bash
#
# One catalog per ID. Not one catalog.
#
# WHAT IT USED TO CATCH, AND STILL DOES.
# `A2UISurfaceView.tsx` and `SurfaceStreamProvider.tsx` each declared
# `ALEPH_V09_CATALOG_ID = "aleph://v1"` and each built its own `new Catalog(...)`
# under that id. They disagreed about exactly one argument: the pane copy passed
# every basic-catalog function, the other passed `[]`. `lib/copilot.tsx` built
# the chat renderer from the empty one, so a surface using `formatDate`,
# `equals` or `openUrl` rendered correctly in a pane and threw
# `Function not found in catalog 'aleph://v1'` in chat — same surface, same id,
# two behaviours, no build error and no test to catch it.
#
# WHAT CHANGED (WS-A3b). "Exactly one catalog" was the wrong rule. A renderer
# holds several NAMED catalogs at once and `createSurface` says which one it
# means, which is what lets two plugins each define a component called `Chart`.
# Aleph now builds core, core's legacy alias, and one catalog per enabled
# plugin. So the rule is no longer "one" — it is:
#
#   1. every catalog id is DECLARED in the canonical module and nowhere else
#   2. no two declared ids have the SAME VALUE (the silent map overwrite)
#   3. every `new Catalog(` is in the canonical module
#   4. every `new Catalog(` gets the basic-catalog functions
#   5. the plugin id template still carries the major, so `@1` and `@2` cannot
#      collapse into one string and take every live surface with them
#
# Rule 2 is the new failure mode and it is the one worth the file: two catalog
# objects claiming one id is a `Map.set` — `new Catalog` does not mind,
# TypeScript does not mind, and `MessageProcessor` silently resolves whichever
# it finds first.
#
# WHAT IT CANNOT SEE. Plugin catalog ids are computed at run time from the
# server's answer, so a duplicate among THOSE is not visible to grep. That case
# is refused at run time by `buildAlephCatalogs`, pinned by
# `apps/web/src/a2ui/aleph-catalog-v09.test.tsx`
# ("refuses two descriptors claiming one catalog id"), and reported server-side
# by `aleph_a2ui.plugin_catalogs.assemble_catalogs`. This sweep covers the
# static half; do not read it as covering both.
#
# Static — no build, no browser, no server.
#
# CI-wired (.github/workflows/ci.yml) and probed by
# scripts/_acceptance/self_check.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SRC="apps/web/src"
CANON="$SRC/a2ui/aleph-catalog-v09.tsx"
fail=0

note() { printf '  %s\n' "$*" >&2; }

[ -f "$CANON" ] || { echo "MISSING: $CANON" >&2; exit 1; }

# 1 — every catalog id declaration lives in the canonical module.
ids="$(grep -rnE '^[[:space:]]*export const [A-Z0-9_]*CATALOG_ID[[:space:]]*=' \
  --include='*.ts' --include='*.tsx' "$SRC" || true)"
stray="$(printf '%s\n' "$ids" | grep -v "^$CANON:" | grep -c . || true)"
if [ "$stray" -ne 0 ]; then
  echo "FAIL: a catalog id is declared outside $CANON" >&2
  printf '%s\n' "$ids" | grep -v "^$CANON:" | while IFS= read -r l; do
    [ -n "$l" ] && note "$l"
  done
  note "a second declaration is how two catalogs came to claim one identity"
  fail=1
fi

# 2 — no two declared ids share a value.
#     The new failure mode: two catalog objects, one id, and whichever the
#     processor finds first wins for every surface that names it.
dupes="$(printf '%s\n' "$ids" \
  | sed -nE 's/.*=[[:space:]]*"([^"]+)".*/\1/p' \
  | sort | uniq -d || true)"
if [ -n "$dupes" ]; then
  echo "FAIL: two catalog ids have the same value" >&2
  printf '%s\n' "$dupes" | while IFS= read -r v; do
    [ -n "$v" ] && note "$v is declared more than once:"
    printf '%s\n' "$ids" | grep -F "\"$v\"" | while IFS= read -r l; do note "    $l"; done
  done
  note "createSurface resolves an id to exactly one catalog; the other is dead"
  fail=1
fi

# 3 — every construction is in the canonical module.
# `strip_comments` drops doc-comment and `//` lines so prose about the rule —
# including this file's own header — never counts as a violation.
strip_comments() { grep -vE '^[^:]+:[0-9]+:[[:space:]]*(\*|//|/\*)' || true; }

ctors="$(grep -rn --include='*.ts' --include='*.tsx' -E '(^|[^.[:alnum:]_])new Catalog\(' "$SRC" \
  | strip_comments || true)"
n_ctors="$(printf '%s' "$ctors" | grep -c . || true)"
outside="$(printf '%s\n' "$ctors" | grep -v "^$CANON:" | grep -c . || true)"
if [ "$n_ctors" -lt 1 ]; then
  echo "FAIL: no 'new Catalog(' found under $SRC — the renderer has no catalog at all" >&2
  fail=1
elif [ "$outside" -ne 0 ]; then
  echo "FAIL: 'new Catalog(' appears outside $CANON" >&2
  printf '%s\n' "$ctors" | grep -v "^$CANON:" | while IFS= read -r l; do
    [ -n "$l" ] && note "$l"
  done
  note "every renderer must call buildAlephCatalogs() from aleph-catalog-v09.tsx"
  fail=1
fi

# 4 — every catalog built carries the basic-catalog FUNCTIONS.
#     This is the argument the two copies disagreed about; dropping it silently
#     breaks every surface that binds a function, and only in some renderers.
if ! grep -q 'basicCatalog\.functions\.values()' "$CANON"; then
  echo "FAIL: the canonical builder no longer passes basicCatalog.functions" >&2
  note "surfaces binding formatDate/equals/openUrl will throw at render time"
  note "see $CANON"
  fail=1
fi
n_funcs="$(grep -c 'coreFunctions()' "$CANON" || true)"
# One definition plus one call per `new Catalog(`. Fewer means a catalog was
# constructed without them — the original defect, reintroduced for one catalog
# out of several, which is the version nobody notices.
if [ "$n_funcs" -lt $((n_ctors + 1)) ]; then
  echo "FAIL: $n_ctors catalogs are built but coreFunctions() appears $n_funcs time(s)" >&2
  note "expected at least one call per constructed catalog, plus its definition"
  note "a catalog built without functions renders in one place and throws in another"
  fail=1
fi

# 5 — the plugin id template still distinguishes majors.
if ! grep -q 'aleph://plugin/${name}@${major}' "$CANON"; then
  echo "FAIL: the plugin catalog id template no longer carries the major version" >&2
  note "\`aleph://plugin/x@1\` and \`@2\` must be different strings, or an upgrade"
  note "silently replaces the catalog every already-open surface is painting with"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "catalog ids: $(printf '%s\n' "$ids" | grep -c .) declared, all unique, all in $CANON; \
$n_ctors catalogs built, functions present"
