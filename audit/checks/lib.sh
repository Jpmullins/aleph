#!/usr/bin/env bash
# Shared helpers for Aleph audit checks.
# Each check sources this, then calls pass/fail/skip which set the exit code
# the harness (run.sh) reads: 0=pass, 1=fail, 2=skip, other=error.
set -uo pipefail

API="${ALEPH_API_BASE_URL:-http://localhost:8000}"
RUNTIME="${ALEPH_COPILOT_RUNTIME_URL:-http://localhost:4000}"
AUTH_HDR="Authorization: Bearer local-dev"
CT_HDR="Content-Type: application/json"
API_CONTAINER="${ALEPH_API_CONTAINER:-compose-aleph-api-1}"

pass() { echo "RESULT:PASS ${*:-ok}"; exit 0; }
fail() { echo "RESULT:FAIL ${*:-assertion failed}"; exit 1; }
skip() { echo "RESULT:SKIP ${*:-precondition unavailable}"; exit 2; }

# api METHOD PATH [JSON-BODY]  -> raw response body on stdout
api() {
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -sS -X "$method" -H "$AUTH_HDR" -H "$CT_HDR" "$API$path" -d "$data"
  else
    curl -sS -X "$method" -H "$AUTH_HDR" "$API$path"
  fi
}

# api_code METHOD PATH [JSON-BODY] -> HTTP status code on stdout
api_code() {
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -sS -o /dev/null -w '%{http_code}' -X "$method" -H "$AUTH_HDR" -H "$CT_HDR" "$API$path" -d "$data"
  else
    curl -sS -o /dev/null -w '%{http_code}' -X "$method" -H "$AUTH_HDR" "$API$path"
  fi
}

# First active project id (any).
first_project() { api GET /v1/projects | jq -r '[.[] | select(.status=="active")][0].id // empty'; }

# Pick an active project whose list endpoint $1 (e.g. "wiki/pages") is non-empty.
pick_project_with() {
  local sub="$1" pid n
  for pid in $(api GET /v1/projects | jq -r '.[] | select(.status=="active") | .id'); do
    n=$(api GET "/v1/projects/$pid/$sub" | jq 'if type=="array" then length else 0 end' 2>/dev/null)
    if [ "${n:-0}" -gt 0 ]; then echo "$pid"; return 0; fi
  done
  return 1
}

# create_project "title" "description" -> project id
#
# `ProjectCreate` is `extra="forbid"`, so an obsolete field is a 422, not a
# silently ignored key. `budget_usd` went with the `budgets` table; the e2e
# twin of this helper (`e2e/helpers.ts`) dropped it at the same time.
create_project() {
  local title="$1" desc="${2:-audit probe}"
  api POST /v1/projects \
    "{\"title\":\"[audit] $title\",\"description\":\"$desc\",\"model_profile_name\":\"aleph-dev\"}" \
    | jq -r '.id'
}

delete_project() { [ -n "${1:-}" ] && api PATCH "/v1/projects/$1" '{"status":"deleted"}' >/dev/null 2>&1 || true; }

# poll_agent_run PROJECT_ID KIND TIMEOUT_SECS -> prints terminal status, rc 0 if found
poll_agent_run() {
  local pid="$1" kind="$2" timeout="${3:-90}" start now st
  start=$(date +%s)
  while :; do
    st=$(api GET "/v1/projects/$pid/agent-runs?limit=50" \
      | jq -r --arg k "$kind" '[.[] | select(.agent_kind==$k and (.status=="succeeded" or .status=="failed" or .status=="cancelled"))][0].status // empty' 2>/dev/null)
    if [ -n "$st" ]; then echo "$st"; return 0; fi
    now=$(date +%s)
    [ $((now - start)) -ge "$timeout" ] && return 1
    sleep 3
  done
}

# Emit one SSE frame from an endpoint or time out. sse_first_frame PATH TIMEOUT_SECS
sse_first_frame() {
  local path="$1" timeout="${2:-12}"
  curl -sS --max-time "$timeout" -N -H "$AUTH_HDR" "$API$path" 2>/dev/null | head -c 400
}
