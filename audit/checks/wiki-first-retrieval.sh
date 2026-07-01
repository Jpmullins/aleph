#!/usr/bin/env bash
# claim: wiki-first-retrieval (http)
source "$(dirname "$0")/lib.sh"

pid=$(pick_project_with wiki/pages) || skip "no project has wiki pages to retrieve over"
resp=$(api POST "/v1/projects/$pid/retrieval/debug" '{"query":"What is this project about?"}')
body=$(echo "$resp" | jq -r '.composed_body_md // ""')
len=${#body}
[ "$len" -ge 40 ] || fail "retrieval composed_body_md too short ($len chars): $(echo "$resp" | head -c 160)"
pass "wiki-first retrieval composed a $len-char answer for project $pid"
