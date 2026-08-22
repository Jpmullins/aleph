"""The coverage floor may not be lowered quietly.

Acceptance row E12 runs `pnpm -C apps/web test:coverage` and reads the
statements percentage. That proves the mechanism WORKS — @vitest/coverage-v8
is installed, the thresholds are evaluated, a dropped suite reddens it — and
it proves nothing about the floor itself. Measured: set all four thresholds
in `apps/web/vitest.config.ts` to 0 and the row is still green, because the
command still prints a percentage and still exits 0.

So the one move that makes a red build green — lowering the floor — was
invisible to every gate in the repository. This pins the four numbers.

Raising a threshold is fine and needs no edit here; the check is one-sided on
purpose, because a ratchet that has to be updated to tighten is a ratchet
people stop tightening. Lowering one requires changing the pin in the same
commit, which is the point: it becomes a decision somebody made rather than a
number that drifted.
"""

from __future__ import annotations

import pathlib
import re
import sys

CONFIG = pathlib.Path("apps/web/vitest.config.ts")

#: The floor as of 2026-08-22, measured at statements 39.47% (649/1644),
#: branches 26.33%, functions 32.53%, lines 40.69% — each threshold set about
#: 1.5 points under the real figure so ordinary churn does not redden CI.
PINNED = {"statements": 38, "branches": 24, "functions": 31, "lines": 39}


def main() -> int:
    if not CONFIG.is_file():
        print(f"FAIL: {CONFIG} is gone — move this check with the config")
        return 1

    text = CONFIG.read_text(encoding="utf-8")
    pattern = r"(statements|branches|functions|lines): (\d+),"
    found = {k: int(v) for k, v in re.findall(pattern, text)}

    missing = sorted(set(PINNED) - set(found))
    if missing:
        print(f"FAIL: no threshold found for {missing} in {CONFIG} — was the block removed?")
        return 1

    lowered = {k: (found[k], PINNED[k]) for k in PINNED if found[k] < PINNED[k]}
    if lowered:
        for key, (now, floor) in sorted(lowered.items()):
            print(f"FAIL: {key} threshold lowered {floor} -> {now}")
        print(
            "the coverage floor was lowered. If that is deliberate, change PINNED in "
            "this file in the same commit and say why in the message"
        )
        return 1

    raised = [k for k in PINNED if found[k] > PINNED[k]]
    note = f"; raised: {', '.join(sorted(raised))}" if raised else ""
    print(
        "coverage floor intact: "
        + ", ".join(f"{k} {found[k]}" for k in ("statements", "branches", "functions", "lines"))
        + note
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
