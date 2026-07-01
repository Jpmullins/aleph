#!/usr/bin/env bash
# Not a claim check — shared seeding helper sourced by promote-dependent checks.
# seed_promoted PID -> echoes "<proposal_id> <page_id>"; returns 1 on failure.
seed_promoted() {
  local pid="$1" nid pr
  nid=$(api POST "/v1/projects/$pid/notes" '{"title":"audit seed note"}' | jq -r '.id')
  [ -n "$nid" ] && [ "$nid" != "null" ] || return 1
  api POST "/v1/projects/$pid/notes/$nid/sections" \
    '{"body_md":"Knowledge distillation trains a compact student model to mimic a larger teacher via soft targets, transferring capability at lower inference cost."}' >/dev/null
  pr=$(api POST "/v1/projects/$pid/notes/$nid/promote" '{}')
  local prop page
  prop=$(echo "$pr" | jq -r '.proposal_id // empty')
  page=$(echo "$pr" | jq -r '.page_id // empty')
  [ -n "$prop" ] && [ -n "$page" ] || return 1
  echo "$prop $page"
}
