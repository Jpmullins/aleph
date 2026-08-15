#!/usr/bin/env bash
# claim: project-soft-delete (dataflow)
source "$(dirname "$0")/lib.sh"

pid=$(create_project "soft-delete probe")
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create probe project"

# It should appear in the active list first.
api GET /v1/projects | jq -e --arg id "$pid" 'any(.[]; .id==$id and .status=="active")' >/dev/null \
  || { delete_project "$pid"; fail "freshly created project not in active list"; }

code=$(api_code PATCH "/v1/projects/$pid" '{"status":"deleted"}')
[ "$code" = "200" ] || fail "soft-delete PATCH returned HTTP $code"

# Now it must be gone from the active list.
still=$(api GET /v1/projects | jq --arg id "$pid" 'any(.[]; .id==$id and .status=="active")')
[ "$still" = "false" ] || fail "project still active after soft-delete"
pass "project $pid created, appeared active, then removed from active list on delete"
