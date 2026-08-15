#!/usr/bin/env bash
# claim: hypotheses-create (dataflow)
source "$(dirname "$0")/lib.sh"

pid=$(create_project "hypotheses probe")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

title="H-$$-$RANDOM"
code=$(api_code POST "/v1/projects/$pid/hypotheses" "{\"title\":\"$title\",\"statement\":\"audit probe statement\"}")
[ "$code" = "201" ] || fail "hypothesis create returned HTTP $code"

found=$(api GET "/v1/projects/$pid/hypotheses" | jq --arg t "$title" 'any(.[]; .title==$t)')
[ "$found" = "true" ] || fail "created hypothesis '$title' not present in list"
pass "hypothesis '$title' created and listed"
