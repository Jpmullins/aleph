#!/usr/bin/env bash
# claim: briefs-pending-proposals (dataflow)
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/_seed.sh"

pid=$(create_project "briefs probe" "Knowledge distillation for compressing LLMs.")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

# Before: empty briefs.
b0=$(api GET "/v1/projects/$pid/briefs" | jq '.badge_count')

read -r prop page < <(seed_promoted "$pid") || fail "seed promote failed"

briefs=$(api GET "/v1/projects/$pid/briefs")
badge=$(echo "$briefs" | jq '.badge_count')
[ "${badge:-0}" -ge 1 ] || fail "briefs badge_count is $badge after creating a pending proposal (was $b0)"
echo "$briefs" | jq -e '[.. | .type? // empty] | any(. == "ApprovalCard")' >/dev/null 2>&1 \
  || fail "briefs surface has no ApprovalCard for the pending proposal"
pass "pending proposal surfaced in Briefs (badge $b0 -> $badge) as an ApprovalCard"
