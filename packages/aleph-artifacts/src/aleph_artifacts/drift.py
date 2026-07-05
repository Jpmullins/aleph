"""Artifact drift detection (WP-6 §5).

An artifact records, in ``ArtifactVersion.lineage_jsonb["source_pages"]``, the
wiki pages it drew from and the exact revision of each *at build time*:
``[{page_id, revision_id, revision_created_at}]``.

``is_drifted`` is a **pure function**: an artifact is *drifted* iff any recorded
upstream page's *current* ``current_revision_id`` differs from the
``revision_id`` the artifact recorded (a newer revision was committed since the
build). Drift is never stored — it is always live-computed from the current wiki
graph, so it cannot itself go stale.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


def is_drifted(
    source_pages: list[dict[str, Any]] | None,
    current_revisions: dict[UUID, UUID | None],
) -> bool:
    """True iff any recorded source page's current revision moved past the
    recorded one.

    ``source_pages`` is the ``lineage_jsonb["source_pages"]`` list (each entry a
    ``{page_id, revision_id, ...}`` dict; ``page_id``/``revision_id`` are UUID
    strings). ``current_revisions`` maps ``page_id -> current_revision_id`` for
    the pages as they exist *now* (a missing page → treated as drifted, since the
    upstream it was built from is gone). An artifact with no recorded source
    pages never drifts.
    """
    for entry in source_pages or []:
        raw_page = entry.get("page_id")
        raw_rev = entry.get("revision_id")
        if raw_page is None:
            continue
        page_id = raw_page if isinstance(raw_page, UUID) else UUID(str(raw_page))
        recorded_rev = None if raw_rev is None else UUID(str(raw_rev))
        if page_id not in current_revisions:
            # The contributing page no longer exists (merged/deleted): drifted.
            return True
        if current_revisions[page_id] != recorded_rev:
            return True
    return False
