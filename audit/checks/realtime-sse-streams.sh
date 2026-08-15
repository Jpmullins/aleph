#!/usr/bin/env bash
# claim: realtime-sse-streams (http)
source "$(dirname "$0")/lib.sh"

pid=$(first_project) || skip "no projects"
declare -a streams=("surfaces/wiki/stream" "changes/stream" "agent-events/stream")
for s in "${streams[@]}"; do
  frame=$(sse_first_frame "/v1/projects/$pid/$s" 12)
  [ -n "$frame" ] || fail "$s emitted no SSE frame within 12s"
done
# The surfaces stream must emit a real A2UI surface payload, not just a heartbeat.
surf=$(sse_first_frame "/v1/projects/$pid/surfaces/wiki/stream" 12)
echo "$surf" | grep -q "createSurface" || fail "surfaces/wiki/stream did not emit a createSurface frame"
pass "changes + agent-events heartbeat; surfaces/wiki emitted a createSurface frame"
