#!/usr/bin/env bash
# claim: library-source-viewer (http)
source "$(dirname "$0")/lib.sh"

pid=$(pick_project_with sources) || skip "no project has ingested sources"
sid=$(api GET "/v1/projects/$pid/sources" | jq -r '.[0].id')

# Raw asset -> presigned URL
asset=$(api GET "/v1/projects/$pid/sources/$sid/asset")
url=$(echo "$asset" | jq -r '.url // .asset_url // empty')
[ -n "$url" ] || fail "source $sid has no downloadable asset URL: $(echo "$asset" | head -c 160)"

# Normalized text
normcode=$(api_code GET "/v1/projects/$pid/sources/$sid/normalized")
[ "$normcode" = "200" ] || fail "normalized text for source $sid returned HTTP $normcode"

pass "source $sid exposes asset URL + normalized text (HTTP 200)"
