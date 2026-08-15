#!/usr/bin/env bash
# claim: api-imports-resolve (static)
# Builds the FastAPI app inside the running api container: proves every router
# module and its transitive imports resolve (no unresolved imports / broken modules).
source "$(dirname "$0")/lib.sh"

docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${API_CONTAINER}$" || skip "api container ${API_CONTAINER} not running"

out=$(docker exec -w /app "$API_CONTAINER" sh -c \
  'uv run python -c "from aleph_api.main import create_app; a=create_app(); print(\"ROUTES\", len(a.routes))"' 2>&1)
rc=$?
if [ $rc -ne 0 ] || ! echo "$out" | grep -q "ROUTES"; then
  echo "$out" | tail -5
  fail "create_app() failed to build (import/module error)"
fi
n=$(echo "$out" | grep -oE 'ROUTES [0-9]+' | awk '{print $2}')
pass "aleph_api.main:create_app builds with $n routes (all imports resolve)"
