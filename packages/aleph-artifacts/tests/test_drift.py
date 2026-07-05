"""Pure drift computation (WP-6 §5): `is_drifted`.

An artifact is drifted iff any recorded upstream page's *current*
`current_revision_id` differs from the `revision_id` it recorded at build time.
No DB, no wall-clock — a pure compare over the lineage snapshot + current graph.
"""

from __future__ import annotations

from uuid import uuid4

from aleph_artifacts.drift import is_drifted


def test_no_source_pages_never_drifts() -> None:
    assert is_drifted(None, {}) is False
    assert is_drifted([], {uuid4(): uuid4()}) is False


def test_unchanged_revision_is_not_drifted() -> None:
    page = uuid4()
    rev = uuid4()
    sps = [{"page_id": str(page), "revision_id": str(rev), "revision_created_at": None}]
    assert is_drifted(sps, {page: rev}) is False


def test_newer_revision_drifts() -> None:
    page = uuid4()
    built_rev = uuid4()
    newer_rev = uuid4()
    sps = [{"page_id": str(page), "revision_id": str(built_rev)}]
    assert is_drifted(sps, {page: newer_rev}) is True


def test_any_one_page_drifting_drifts_the_artifact() -> None:
    p1, p2 = uuid4(), uuid4()
    r1, r2 = uuid4(), uuid4()
    sps = [
        {"page_id": str(p1), "revision_id": str(r1)},
        {"page_id": str(p2), "revision_id": str(r2)},
    ]
    # p1 unchanged, p2 moved on → drifted.
    assert is_drifted(sps, {p1: r1, p2: uuid4()}) is True


def test_missing_page_counts_as_drift() -> None:
    page = uuid4()
    sps = [{"page_id": str(page), "revision_id": str(uuid4())}]
    # The contributing page no longer exists in the current graph.
    assert is_drifted(sps, {}) is True


def test_accepts_uuid_typed_entries() -> None:
    page = uuid4()
    rev = uuid4()
    sps = [{"page_id": page, "revision_id": rev}]
    assert is_drifted(sps, {page: rev}) is False
    assert is_drifted(sps, {page: uuid4()}) is True
