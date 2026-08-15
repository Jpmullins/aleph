#!/usr/bin/env bash
# claim: cost-tracking (http)
source "$(dirname "$0")/lib.sh"

found=""; spent=""
for pid in $(api GET /v1/projects | jq -r '.[] | select(.status=="active") | .id'); do
  roll=$(api GET "/v1/projects/$pid/cost")
  s=$(echo "$roll" | jq -r '.spent_usd // "0"')
  if awk "BEGIN{exit !(${s:-0}>0)}"; then found="$pid"; spent="$s"; nphase=$(echo "$roll" | jq '.by_phase | length'); break; fi
done
[ -n "$found" ] || skip "no project has recorded LLM spend yet"
[ "${nphase:-0}" -ge 1 ] || fail "cost rollup for $found has spend=$spent but empty by_phase breakdown"
pass "project $found: spent_usd=$spent across $nphase phase(s)"
