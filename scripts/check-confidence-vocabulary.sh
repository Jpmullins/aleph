#!/usr/bin/env bash
#
# The confidence of a claim was spelled out in four places that did not agree,
# and nothing could tell:
#
#   * the derived engine emits six underscore-spelled states;
#   * the A2UI catalog permitted six DIFFERENT words — `cited`, `uncited`,
#     `retracted`, plus two hyphenated spellings of states the engine has;
#   * `ClaimCard.tsx` branched on four literals and rendered the rest grey, so a
#     claim the evidence had DISPROVED looked like one nobody had assessed;
#   * `GroundingSurface.tsx` keyed its badge on `cited | inferred | contested |
#     retracted`, of which only `contested` is a confidence at all.
#
# And 806 of 850 live claims carried `cited`, a word in none of those sets: it
# was the column's server default. Every component agreed with itself, the value
# crossed each boundary as free-form text, and the only symptom was a badge with
# the wrong colour.
#
# This sweep reads the canonical enum out of `aleph_core.confidence` and diffs
# every other spelling of the set against it — the catalog (both copies), the
# client union, the client's `switch` branch labels, the grounding badge map and
# the HTML compiler's badge colours. Static: no database, no gateway, no app
# import.
#
# The analyzer is `scripts/_lib/confidence_vocabulary.py`, imported by this sweep
# and by `tests/unit/test_confidence_vocabulary.py` — one implementation, two
# callers, so the gate does not depend on the test suite existing.
#
# Fails on: any reader that recognises a state the enum does not have, or misses
# one it does.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run --quiet python - <<'PY'
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("scripts/_lib").resolve()))

from confidence_vocabulary import run  # noqa: E402

raise SystemExit(run())
PY
