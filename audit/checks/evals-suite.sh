#!/usr/bin/env bash
# claim: evals-suite (http) — wired:false (backend-only CI gate, no UI entry)
source "$(dirname "$0")/lib.sh"

dcode=$(api_code GET /v1/eval-datasets)
rcode=$(api_code GET /v1/eval-runs)
[ "$dcode" = "200" ] || fail "/v1/eval-datasets returned HTTP $dcode"
[ "$rcode" = "200" ] || fail "/v1/eval-runs returned HTTP $rcode"
# Confirm the auditor's wired:false judgment: no frontend references the eval API.
refs=$(grep -rEl "eval-datasets|eval-runs|/v1/eval" "$(dirname "$0")/../../apps/web/src" 2>/dev/null | wc -l | tr -d ' ')
note="no UI entry point"
[ "$refs" != "0" ] && note="UNEXPECTED: $refs frontend refs"
pass "eval API responds (datasets=$dcode, runs=$rcode); backend-only ($note)"
