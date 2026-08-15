#!/usr/bin/env bash
# claim: notes-crud-roundtrip (dataflow)
source "$(dirname "$0")/lib.sh"

pid=$(create_project "notes-crud probe")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

nid=$(api POST "/v1/projects/$pid/notes" '{"title":"audit note"}' | jq -r '.id')
[ -n "$nid" ] && [ "$nid" != "null" ] || fail "note create failed"

marker="AUDIT-MARKER-$$-$RANDOM"
scode=$(api_code POST "/v1/projects/$pid/notes/$nid/sections" "{\"body_md\":\"$marker survives the round trip\"}")
[ "$scode" = "201" ] || fail "section create returned HTTP $scode"

detail=$(api GET "/v1/projects/$pid/notes/$nid")
echo "$detail" | grep -q "$marker" || fail "marker '$marker' not found in note detail after round trip"
pass "note $nid created; section body marker survived read-back"
