#!/usr/bin/env bash
# claim: library-source-viewer (http)
source "$(dirname "$0")/lib.sh"

pid=$(pick_project_with sources) || skip "no project has ingested sources"
sid=$(api GET "/v1/projects/$pid/sources" | jq -r '.[0].id')

# Raw asset streams through the authenticated route (no URL hop)
assetcode=$(api_code GET "/v1/projects/$pid/assets/source/$sid")
[ "$assetcode" = "200" ] || fail "asset stream for source $sid returned HTTP $assetcode"

# Normalized text
normcode=$(api_code GET "/v1/projects/$pid/sources/$sid/normalized")
[ "$normcode" = "200" ] || fail "normalized text for source $sid returned HTTP $normcode"

pass "source $sid streams raw bytes + normalized text (HTTP 200)"
