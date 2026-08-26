#!/usr/bin/env bash
# Every compose service that BUILDS must also pin an `image:` tag.
#
# Without one, compose derives a tag from the project directory name. `docker
# compose build` then writes a tag that a later `up` may not be running, so a
# rebuild silently does nothing and the old container keeps serving.
#
# Measured, not hypothetical: `workers` had no `image:` key, and a stale image
# answered `function 'delegated_subagent_job' not found` through three rebuilds
# while the build reported success each time. The build WAS succeeding — it was
# writing a tag nothing was running.
#
# Reads the RENDERED config, not the source file, so a tag an override drops is
# still caught — the same reason `check-compose-hardening.sh` renders first.
set -euo pipefail

cd "$(dirname "$0")/.."
FILE="deploy/compose/docker-compose.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "skip: docker not available; cannot render the compose config"
  exit 0
fi

RENDERED="$(docker compose -f "$FILE" config 2>/dev/null || true)"
if [ -z "$RENDERED" ]; then
  echo "skip: could not render $FILE"
  exit 0
fi

problems="$(
  printf '%s\n' "$RENDERED" | uv run python -c '
import sys, yaml
doc = yaml.safe_load(sys.stdin) or {}
bad = []
for name, svc in (doc.get("services") or {}).items():
    if svc.get("build") and not svc.get("image"):
        bad.append(name)
for name in sorted(bad):
    print(name)
'
)"

if [ -n "$problems" ]; then
  echo "✗ compose services that build with no pinned image tag:"
  printf '    %s\n' $problems
  echo
  echo "Add \`image: aleph-<service>:local\`. A build that writes a tag nothing runs"
  echo "looks like a successful rebuild and serves the old code."
  exit 1
fi

n="$(printf '%s\n' "$RENDERED" | uv run python -c '
import sys, yaml
doc = yaml.safe_load(sys.stdin) or {}
print(sum(1 for s in (doc.get("services") or {}).values() if s.get("build")))
')"
echo "OK: all $n building compose service(s) pin an image tag"
