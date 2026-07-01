#!/usr/bin/env bash
# claim: wiki-pages-list-and-read (http)
source "$(dirname "$0")/lib.sh"

pid=$(pick_project_with wiki/pages) || skip "no project has wiki pages"
count=$(api GET "/v1/projects/$pid/wiki/pages" | jq 'length')
[ "${count:-0}" -ge 1 ] || fail "wiki pages list empty"
pageid=$(api GET "/v1/projects/$pid/wiki/pages" | jq -r '.[0].id')
detail=$(api GET "/v1/projects/$pid/wiki/pages/$pageid")
# Longest string anywhere in the detail = the revision body markdown.
blen=$(echo "$detail" | jq -r '[.. | strings] | map(length) | max // 0')
[ "${blen:-0}" -ge 30 ] || fail "page $pageid detail has no substantial body (max str=$blen)"
pass "listed $count pages; page $pageid renders a ~$blen-char body"
