#!/usr/bin/env bash
# claim: approval-card-action (dataflow) — human-in-the-loop approve routes through
# the ActionRouter, mutates the proposal, and writes a ledger event.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/_seed.sh"

pid=$(create_project "approval probe" "Knowledge distillation for compressing LLMs.")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

read -r prop page < <(seed_promoted "$pid") || fail "seed promote failed"

led0=$(api GET "/v1/projects/$pid/ledger?limit=500" | jq 'length')

body=$(jq -nc --arg id "$prop" '{surface_kind:"BriefsSurface",action_kind:"approve",params:{target_id:$id,target_kind:"synthesis_proposal"}}')
resp=$(curl -sS -w '\n%{http_code}' -X POST -H "$AUTH_HDR" -H "$CT_HDR" \
  "$API/v1/projects/$pid/cards/actions" -d "$body")
code=$(echo "$resp" | tail -n1)
[ "$code" = "200" ] || fail "approve action returned HTTP $code: $(echo "$resp" | sed '$d' | head -c 160)"

pstatus=$(api GET "/v1/projects/$pid/synthesis-proposals" | jq -r --arg id "$prop" '.[] | select(.id==$id) | .status')
[ "$pstatus" = "approved" ] || fail "proposal $prop status is '$pstatus' after approve, expected approved"

led1=$(api GET "/v1/projects/$pid/ledger?limit=500" | jq 'length')
[ "${led1:-0}" -gt "${led0:-0}" ] || fail "approve wrote no ledger event ($led0 -> $led1)"
pass "ApprovalCard approve: proposal -> approved + ledger grew ($led0 -> $led1)"
