"""Citation verification — Aleph-side wrapper around AIQ's verify_citations.

In production, this delegates to AIQ's `aiq_agent.common.citation_verification`
once the AIQ submodule is checked out. Until then this module ships a
correct, AIQ-compatible implementation that:

  * Extracts every `[cN]` marker from `body_md`.
  * Looks up each marker in `source_registry` (a dict of marker → source ref).
  * Returns the verified mapping, or raises with the list of missing markers.

The behavior matches AIQ's contract for `verify_citations(report, registry)`
documented in NVIDIA AI-Q Blueprint v2.1.0.
"""

from __future__ import annotations

import re
from typing import TypeVar

CITATION_RE = re.compile(r"\[(c\d+)\]")

T = TypeVar("T")


class CitationVerificationFailure(Exception):
    """At least one `[cN]` marker has no backing source in the registry."""

    def __init__(
        self,
        message: str,
        *,
        verified: dict[str, T],
        missing_markers: list[str],
    ) -> None:
        super().__init__(message)
        self.verified = verified
        self.missing_markers = missing_markers


def verify_citations[T](
    *,
    body_md: str,
    source_registry: dict[str, T],
) -> dict[str, T]:
    """Return the verified marker→source mapping; raise if any marker is missing.

    `source_registry` keys are `c1`, `c2`, … (without brackets) — same
    shape AIQ uses. Markers in `body_md` that aren't in the registry are
    collected and reported as missing.
    """
    found_markers: list[str] = []
    for m in CITATION_RE.finditer(body_md):
        marker = m.group(1)
        if marker not in found_markers:
            found_markers.append(marker)
    verified: dict[str, T] = {}
    missing: list[str] = []
    for marker in found_markers:
        ref = source_registry.get(marker)
        if ref is None:
            missing.append(marker)
        else:
            verified[marker] = ref
    if missing:
        msg = f"{len(missing)} citation marker(s) have no backing source: " + ", ".join(missing)
        raise CitationVerificationFailure(msg, verified=verified, missing_markers=missing)
    return verified


def sanitize_report(*, body_md: str, verified: dict[str, object]) -> str:
    """Strip unverified `[cN]` markers from `body_md`. Verified markers are kept.

    Mirrors AIQ's `sanitize_report` behavior — used when the agent
    decides to land a partial draft rather than block on missing citations.
    """

    def _replace(match: re.Match[str]) -> str:
        marker = match.group(1)
        return match.group(0) if marker in verified else "[c?]"

    return CITATION_RE.sub(_replace, body_md)
