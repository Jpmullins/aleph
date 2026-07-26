#!/usr/bin/env bash
#
# The A2UI catalog has exactly one editable copy: `catalog.json`. Everything
# else is generated from it.
#
# It was previously maintained by hand in three places — a 614-line
# `catalog.py`, `apps/web/src/a2ui/catalog.ts`, and a ~265-line object literal
# inside `copilot-runtime/src/server.ts`. They drifted invisibly:
# `ClaimCard.confidence` listed no `"cited"` in any copy (the value both wiki
# writers hardcode, so validating a real card would have rejected it); the
# agent-facing copy offered `"initial"`, recognised by nothing, while omitting
# `"retracted"`, making the WP-6 state unemittable.
#
# `check-catalog-roster.sh` catches a component without a renderer or producer.
# This one catches something narrower and more fundamental: a generated file
# that no longer matches the source it was generated from.
#
# CI-wired. Fails on: an edit to a generated catalog, or an edit to
# `catalog.json` without re-running the generator.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run --quiet python scripts/gen_catalog.py --check
