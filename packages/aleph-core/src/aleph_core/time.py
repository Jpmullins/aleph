"""Timezone-aware UTC time utilities. All Aleph timestamps are UTC."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time as a tz-aware datetime."""
    return datetime.now(UTC)
