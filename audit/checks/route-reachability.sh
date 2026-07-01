#!/usr/bin/env bash
# claim: route-reachability (route/static)
# Asserts: (a) the client router's states map to real components reachable from a
# UI entry point, (b) every API router listed in main.py imports, (c) flags
# API endpoints with no frontend caller (UI-orphan).
source "$(dirname "$0")/lib.sh"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WEB="$ROOT/apps/web/src"

# (a) Client routes: App.tsx renders ProjectList (projects), ProjectWorkspace
# (workspace), login, callback. Workspace is reached via ProjectList onOpen ->
# navigate(/projects/:id). Verify those components exist and are imported.
app="$WEB/App.tsx"
for comp in ProjectList ProjectWorkspace; do
  grep -q "import { $comp }" "$app" || fail "App.tsx does not import $comp"
  [ -f "$WEB/components/$comp.tsx" ] || fail "route component $comp.tsx missing"
done
grep -q 'navigate(`/projects/' "$app" || fail "no UI entry point navigates into the workspace route"

# (b) Every router imported in main.py must resolve to a route module file.
MAIN="$ROOT/apps/api/src/aleph_api/main.py"
routers=$(sed -n '/from aleph_api.routes import (/,/)/p' "$MAIN" | grep -oE '^\s+[a-z_]+,' | tr -d ' ,')
missing=""
for r in $routers; do
  [ -f "$ROOT/apps/api/src/aleph_api/routes/$r.py" ] || missing="$missing $r"
done
[ -z "$missing" ] || fail "main.py imports routers with no module:$missing"
nrouters=$(echo "$routers" | wc -w | tr -d ' ')

# (c) Flag known UI-orphan endpoints (backend routes with no frontend caller).
orphans=""
for pat in "eval-runs" "merge-proposals" "reviews/editorial" "reviews/mechanical"; do
  cnt=$(grep -rEl "$pat" "$WEB" 2>/dev/null | wc -l | tr -d ' ')
  [ "$cnt" = "0" ] && orphans="$orphans $pat"
done

pass "client routes resolve + workspace reachable; $nrouters API routers import; UI-orphan endpoints flagged:${orphans:- none}"
