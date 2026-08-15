#!/usr/bin/env bash
# claim: bootstrap-on-create (dataflow, slow)
source "$(dirname "$0")/lib.sh"
TIMEOUT="${BOOTSTRAP_TIMEOUT:-150}"

docker ps --format '{{.Names}}' 2>/dev/null | grep -q workers || skip "workers container not running"

pid=$(create_project "bootstrap probe" "Retrieval-augmented generation grounds LLM answers in an external corpus to reduce hallucination.")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

# A bootstrap agent run should appear quickly if auto-bootstrap is enabled.
seen_run=""; overview=""
start=$(date +%s)
while :; do
  runs=$(api GET "/v1/projects/$pid/agent-runs?limit=50")
  echo "$runs" | jq -e 'any(.[]; .agent_kind=="bootstrap")' >/dev/null 2>&1 && seen_run=1
  overview=$(api GET "/v1/projects/$pid/wiki/pages" | jq -r '[.[] | select(.page_kind=="overview")][0].id // empty')
  [ -n "$overview" ] && break
  now=$(date +%s); [ $((now - start)) -ge "$TIMEOUT" ] && break
  sleep 4
done

if [ -z "$seen_run" ] && [ -z "$overview" ]; then
  fail "no bootstrap agent run and no overview page after ${TIMEOUT}s (auto-bootstrap not delivering — check bootstrap_auto_enabled)"
fi
[ -n "$overview" ] || fail "bootstrap run seen but no overview page seeded after ${TIMEOUT}s"
blen=$(api GET "/v1/projects/$pid/wiki/pages/$overview" | jq -r '[.. | strings] | map(length) | max // 0')
[ "${blen:-0}" -ge 30 ] || fail "overview page $overview has no substantial body"
pass "project auto-bootstrapped: overview page $overview seeded (body ~$blen chars) in $(( $(date +%s) - start ))s"
