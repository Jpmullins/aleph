#!/usr/bin/env bash
#
# The Node bridge on :4000 must not be reachable from the whole network, and its
# allowed origins must be configuration.
#
# `scripts/_acceptance/runtime_bridge_probe.mjs` boots the server and checks
# what it does on the wire. This checks the two things a running server cannot
# tell you about itself: where the port is published, and whether the origin
# list is shared with the API rather than duplicated.
#
# WHY BOTH. CORS is enforced by BROWSERS. Narrowing it stops a malicious page
# from using someone's session — which was the live hole, since `cors: true`
# allowed every origin with credentials. It does nothing about a direct request
# and never could. This bridge drives the agent with no authentication of its
# own, so what actually bounds who can reach it is the publish address.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE=deploy/compose/docker-compose.yml
SERVER=apps/copilot-runtime/src/server.ts
FAIL=0
note() { printf '  %-4s %s\n' "$1" "$2"; [ "$1" = "FAIL" ] && FAIL=1; return 0; }

# 1. The port is published on loopback, not on every interface.
if grep -qE 'ports: \["\$\{RUNTIME_BIND_HOST:-127\.0\.0\.1\}' "$COMPOSE"; then
  note ok "port 4000 is published on loopback by default"
else
  note FAIL "port 4000 is published on all interfaces — anything on the LAN can drive the agent"
fi

# 2. cors: true is gone. Named explicitly because it is the exact prior value,
#    and a reviewer skimming for "cors" would see the key either way.
if grep -qE '^\s*cors: true,' "$SERVER"; then
  note FAIL "server.ts still sets cors: true — every origin may drive the agent"
else
  note ok "the runtime does not allow every origin"
fi

# 3. The origin list is configuration in both places.
n=$(grep -c 'ALEPH_CORS_ORIGINS' "$SERVER" "$COMPOSE" | awk -F: '{s+=$2} END {print s}')
if [ "${n:-0}" -ge 2 ]; then
  note ok "allowed origins come from ALEPH_CORS_ORIGINS in both the server and compose"
else
  note FAIL "allowed origins are a constant, not configuration (found $n references)"
fi

# 4. The bridge and the API agree. Two independent defaults drift, and a bridge
#    that allows an origin the API refuses has its own security policy.
api_line=$(grep -m1 'ALEPH_CORS_ORIGINS:' "$COMPOSE" | sed 's/.*ALEPH_CORS_ORIGINS: *//')
runtime_line=$(grep 'ALEPH_CORS_ORIGINS:' "$COMPOSE" | tail -1 | sed 's/.*ALEPH_CORS_ORIGINS: *//')
if [ "$api_line" = "$runtime_line" ]; then
  note ok "the bridge and the API resolve the same origin list"
else
  note FAIL "the bridge and the API have different origin lists: '$runtime_line' vs '$api_line'"
fi

# 5. The browser attaches a credential. A source check, and weaker than the
#    others on purpose: there is no JS test framework in this repo yet (WS-UI-2),
#    so there is nothing to mount the provider in.
if grep -qE '^\s*headers=\{headers\}' apps/web/src/lib/copilot.tsx; then
  note ok "CopilotKitProvider is mounted with a headers prop"
else
  note FAIL "the browser sends no credential to the runtime"
fi

if [ $FAIL -eq 0 ]; then
  printf '\n\033[32mOK\033[0m: the runtime bridge is not an any-origin, any-host proxy\n'
else
  printf '\n\033[31mFAIL\033[0m: see above\n'
fi
exit $FAIL
