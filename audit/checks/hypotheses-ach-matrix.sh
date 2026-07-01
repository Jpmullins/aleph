#!/usr/bin/env bash
# claim: hypotheses-ach-matrix (http)
source "$(dirname "$0")/lib.sh"

pid=$(first_project) || skip "no projects"
resp=$(api GET "/v1/projects/$pid/hypotheses/ach")
# The ACH matrix contract: hypotheses[], targets[], cells[], fewest_disconfirming_id.
for key in hypotheses targets cells; do
  echo "$resp" | jq -e --arg k "$key" '.[$k] | type=="array"' >/dev/null 2>&1 \
    || fail "ACH matrix missing array field '$key': $(echo "$resp" | head -c 160)"
done
echo "$resp" | jq -e 'has("fewest_disconfirming_id")' >/dev/null 2>&1 \
  || fail "ACH matrix missing fewest_disconfirming_id"
pass "ACH matrix returns the full contract (hypotheses/targets/cells/fewest_disconfirming_id)"
