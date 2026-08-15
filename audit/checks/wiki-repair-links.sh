#!/usr/bin/env bash
# claim: wiki-repair-links (http)
source "$(dirname "$0")/lib.sh"

pid=$(pick_project_with wiki/pages) || skip "no project has wiki pages"
resp=$(curl -sS -w '\n%{http_code}' -X POST -H "$AUTH_HDR" -H "$CT_HDR" \
  "$API/v1/projects/$pid/wiki/aliases/repair-links" -d '{}')
code=$(echo "$resp" | tail -n1)
body=$(echo "$resp" | sed '$d')
[ "$code" = "200" ] || fail "repair-links returned HTTP $code: $(echo "$body" | head -c 160)"
# Response should carry some notion of how many links were repaired/scanned.
echo "$body" | jq -e 'type=="object"' >/dev/null 2>&1 || fail "repair-links did not return a JSON object: $(echo "$body" | head -c 120)"
pass "repair-links 200; payload=$(echo "$body" | jq -c '.' | head -c 160)"
