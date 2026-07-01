#!/usr/bin/env bash
# claim: notes-promote-to-wiki (dataflow)
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/_seed.sh"

pid=$(create_project "promote probe" "Knowledge distillation for compressing LLMs.")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

read -r prop page < <(seed_promoted "$pid") || fail "promote did not return proposal_id + page_id"

# A pending synthesis proposal must now exist.
pstatus=$(api GET "/v1/projects/$pid/synthesis-proposals" | jq -r --arg id "$prop" '.[] | select(.id==$id) | .status')
[ "$pstatus" = "pending" ] || fail "promoted proposal $prop status is '$pstatus', expected pending"

# The referenced wiki page must exist (flat list carries id/status/page_kind).
row=$(api GET "/v1/projects/$pid/wiki/pages" | jq -c --arg id "$page" '.[] | select(.id==$id)')
[ -n "$row" ] || fail "promoted page $page not present in wiki pages list"
kind=$(echo "$row" | jq -r '.page_kind')
status=$(echo "$row" | jq -r '.status')
pass "note promoted -> page $page (kind=$kind status=$status) + pending proposal $prop"
