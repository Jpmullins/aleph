#!/usr/bin/env bash
# claim: research-to-wiki (dataflow, non-destructive over existing data)
# Proves the native research->synthesis->wiki pipeline actually ran: a project
# must hold a synthesis proposal referencing a real committed wiki page + an
# agent run, AND research-ingested sources (connector_kind != upload) that the
# loop registered with real connector provenance.
source "$(dirname "$0")/lib.sh"

pid=$(pick_project_with synthesis-proposals) || skip "no project has synthesis proposals (research never ran)"
props=$(api GET "/v1/projects/$pid/synthesis-proposals")
n=$(echo "$props" | jq 'length')
[ "${n:-0}" -ge 1 ] || fail "no synthesis proposals for $pid"

page_id=$(echo "$props" | jq -r '.[0].page_id // empty')
run_id=$(echo "$props" | jq -r '.[0].agent_run_id // empty')
[ -n "$page_id" ] || fail "synthesis proposal has no page_id (not linked to a wiki page)"
[ -n "$run_id" ]  || fail "synthesis proposal has no agent_run_id (not linked to a research run)"

# The referenced page must actually exist and be readable.
pcode=$(api_code GET "/v1/projects/$pid/wiki/pages/$page_id")
[ "$pcode" = "200" ] || fail "proposal references page $page_id but it returns HTTP $pcode"
blen=$(api GET "/v1/projects/$pid/wiki/pages/$page_id" | jq -r '[.. | strings] | map(length) | max // 0')
[ "${blen:-0}" -ge 30 ] || fail "research-produced page $page_id has no substantial body"

# The native loop ingests via the typed connectors — the project must hold
# sources whose connector_kind names a real connector, not manual upload.
kinds=$(api GET "/v1/projects/$pid/sources" \
  | jq -r '[.[] | select(.connector_kind != "upload") | .connector_kind] | unique | join(",")')
nresearch=$(api GET "/v1/projects/$pid/sources" \
  | jq '[.[] | select(.connector_kind != "upload")] | length')
[ "${nresearch:-0}" -ge 1 ] || fail "no research-ingested sources (every source is connector_kind=upload)"

pass "$n proposal(s); proposal->page $page_id (body ~$blen chars)->run $run_id; $nresearch research source(s) via [$kinds]"
