#!/usr/bin/env bash
# claim: artifacts-build (dataflow, slow)
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/_seed.sh"
TIMEOUT="${BUILD_TIMEOUT:-180}"

docker ps --format '{{.Names}}' 2>/dev/null | grep -q workers || skip "workers container not running"

pid=$(create_project "artifact probe" "Knowledge distillation for compressing LLMs.")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

# Produce a wiki page to build from (promote a note, then approve it).
read -r prop page < <(seed_promoted "$pid") || fail "seed promote failed"
api_code POST "/v1/projects/$pid/wiki/pages/$page/approve" '{}' >/dev/null

body=$(jq -nc --arg p "$page" '{title:"Audit bundle",artifact_kind:"report_markdown_bundle",wiki_page_ids:[$p]}')
resp=$(api POST "/v1/projects/$pid/artifacts/build" "$body")
aid=$(echo "$resp" | jq -r '.artifact_id // empty')
[ -n "$aid" ] || fail "artifacts/build did not return artifact_id: $(echo "$resp" | head -c 160)"

st=$(poll_agent_run "$pid" builder "$TIMEOUT") || fail "builder agent run did not reach terminal state in ${TIMEOUT}s"
[ "$st" = "succeeded" ] || fail "builder run terminal status is '$st', expected succeeded"

art=$(api GET "/v1/projects/$pid/artifacts/$aid")
ver=$(echo "$art" | jq -r '.current_version_id // empty')
[ -n "$ver" ] || fail "built artifact $aid has no current_version_id (nothing to download)"
# The frontend Download button (ArtifactsSurface.tsx:266) links here.
dcode=$(api_code GET "/v1/projects/$pid/artifacts/$aid/versions/$ver/download")
case "$dcode" in
  200|302|307) pass "artifact $aid built (builder succeeded) + downloadable (HTTP $dcode)" ;;
  *) fail "artifact BUILT ok, but the download the UI links to returns HTTP $dcode — /artifacts/{id}/versions/{ver}/download route does not exist (unwired download)" ;;
esac
