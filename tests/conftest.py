"""Repo-wide test fixtures and path setup.

`scripts/_lib` holds analysis shared between the CI sweeps in `scripts/` and the
tests that prove those sweeps model reality. It is not an installed package —
the sweeps run it by path — so tests that exercise it need it importable.
"""

from __future__ import annotations

import pathlib
import sys

_LIB = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "_lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
