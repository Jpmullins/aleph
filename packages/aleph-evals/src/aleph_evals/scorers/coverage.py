"""Wiki coverage — given a source corpus + ground-truth concepts, the wiki must
hold a page per concept."""

from __future__ import annotations

from typing import Any


def score_coverage(case: dict[str, Any], *, profile_name: str) -> tuple[bool, float | None]:
    expected = set(case.get("expected_concepts") or [])
    actual = set((case.get("actual") or {}).get("wiki_page_titles") or [])
    if not expected:
        return True, None
    hits = sum(1 for c in expected if any(c.lower() in t.lower() for t in actual))
    coverage = hits / len(expected)
    threshold = float(case.get("threshold", 0.9))
    return coverage >= threshold, coverage
