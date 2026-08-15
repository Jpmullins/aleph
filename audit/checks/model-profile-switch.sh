#!/usr/bin/env bash
# claim: model-profile-switch (dataflow)
source "$(dirname "$0")/lib.sh"

pid=$(create_project "profile-switch probe")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

before=$(api GET "/v1/projects/$pid/ledger?limit=200" | jq 'length')
code=$(api_code POST "/v1/projects/$pid/model-profile/switch" '{"profile_name":"aleph-production"}')
[ "$code" = "200" ] || fail "profile switch returned HTTP $code"

now=$(api GET "/v1/projects/$pid/model-profile" | jq -r '.name // .profile_name // empty')
echo "$now" | grep -qi "production" || fail "model-profile did not reflect the switch (got '$now')"

after=$(api GET "/v1/projects/$pid/ledger?limit=200" | jq 'length')
[ "${after:-0}" -gt "${before:-0}" ] || fail "profile switch wrote no ledger event ($before -> $after)"
pass "switched to aleph-production; profile reflects it + ledger grew ($before -> $after)"
