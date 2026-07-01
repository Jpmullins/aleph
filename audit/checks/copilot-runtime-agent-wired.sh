#!/usr/bin/env bash
# claim: copilot-runtime-agent-wired (http)
source "$(dirname "$0")/lib.sh"

info=$(curl -sS --max-time 8 "$RUNTIME/api/copilotkit/info" 2>/dev/null) || skip "copilot-runtime :4000 unreachable"
[ -n "$info" ] || skip "copilot-runtime returned nothing"

echo "$info" | jq -e '.agents.assistant' >/dev/null 2>&1 || fail "runtime /info does not expose the 'assistant' agent"
a2ui=$(echo "$info" | jq -r '(.a2uiEnabled // .a2ui.enabled) // false')
[ "$a2ui" = "true" ] || fail "runtime reports a2ui disabled"

code=$(api_code POST /copilotkit/agent/assistant '{}')
[ "$code" = "404" ] && fail "API AG-UI endpoint /copilotkit/agent/assistant not mounted (404)"

pass "runtime exposes assistant (a2ui=$a2ui); API endpoint mounted (POST->$code)"
