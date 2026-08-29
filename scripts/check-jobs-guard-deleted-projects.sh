#!/usr/bin/env bash
# Every project-scoped worker job refuses a project somebody deleted.
#
# The rule exists because its absence cost $141.43 in sixty minutes, spent on
# `[e2e]` fixture projects that were already `status = 'deleted'`. See
# `apps/workers/src/aleph_workers/project_guard.py` for the measurement.
set -euo pipefail
cd "$(dirname "$0")/.."

out=$(uv run --quiet python -c '
import pathlib, sys
sys.path.insert(0, "scripts/_lib")
from jobs_guard_deleted_projects import violations
problems = violations(pathlib.Path("."))
for p in problems:
    print(p)
sys.exit(1 if problems else 0)
') || {
  echo "FAIL: a worker job would spend money on a deleted project"
  echo "$out" | sed "s/^/  - /"
  exit 1
}
n=$(grep -rl "refuse_if_project_is_gone" apps/workers/src/aleph_workers/jobs/*.py | wc -l | tr -d " ")
echo "OK: every project-scoped job refuses a deleted project ($n job modules guarded)"
