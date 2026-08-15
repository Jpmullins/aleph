#!/usr/bin/env bash
# claim: action-ledger-hashchain (http)
source "$(dirname "$0")/lib.sh"

any=""
for pid in $(api GET /v1/projects | jq -r '.[] | select(.status=="active") | .id'); do
  v=$(api GET "/v1/projects/$pid/ledger/verify")
  ok=$(echo "$v" | jq -r '.ok // false')
  cnt=$(echo "$v" | jq -r '.count // 0')
  div=$(echo "$v" | jq -r '.first_divergence_event_id // "null"')
  any=1
  if [ "$ok" = "true" ] && [ "${cnt:-0}" -gt 0 ] && [ "$div" = "null" ]; then
    pass "hash chain intact for $pid: ok=true, count=$cnt, no divergence"
  fi
done
[ -n "$any" ] || skip "no projects to verify"
fail "no project has a verifiable non-empty ledger hash chain"
