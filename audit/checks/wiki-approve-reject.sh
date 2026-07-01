#!/usr/bin/env bash
# claim: wiki-approve-reject (dataflow)
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/_seed.sh"

pid=$(create_project "wiki-approve probe" "Knowledge distillation for compressing LLMs.")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

read -r prop page < <(seed_promoted "$pid") || fail "seed promote failed"

pstatus() { api GET "/v1/projects/$pid/wiki/pages" | jq -r --arg id "$page" '.[] | select(.id==$id) | .status'; }

# The promoted page starts as a draft.
s0=$(pstatus)
[ "$s0" = "draft" ] || fail "promoted page $page is '$s0', expected draft"

code=$(api_code POST "/v1/projects/$pid/wiki/pages/$page/approve" '{}')
[ "$code" = "200" ] || fail "approve endpoint returned HTTP $code"

s1=$(pstatus)
[ "$s1" = "approved" ] || fail "page $page status is '$s1' after approve, expected approved"
pass "draft page $page approved via endpoint (draft -> approved)"
