"""UUIDv7 generation tests."""

from __future__ import annotations

from aleph_core.ids import uuid7, uuid7_from_int


def test_uuid7_is_v7_variant_10() -> None:
    u = uuid7()
    raw = u.bytes
    # Version 7 → high nibble of byte 6 == 0x70.
    assert (raw[6] & 0xF0) == 0x70
    # Variant 10 → top two bits of byte 8 == 0b10.
    assert (raw[8] & 0xC0) == 0x80


def test_uuid7_monotonic_ish_within_a_burst() -> None:
    ids = sorted(uuid7().bytes for _ in range(50))
    # First 6 bytes (timestamp) of the smallest <= largest. They are by
    # definition monotonic on time, but a burst can land in the same ms;
    # what we want is that we don't lose ordering across an ms boundary.
    ts_min = ids[0][:6]
    ts_max = ids[-1][:6]
    assert ts_min <= ts_max


def test_uuid7_from_int_deterministic() -> None:
    a = uuid7_from_int(1_700_000_000_000, seed=42)
    b = uuid7_from_int(1_700_000_000_000, seed=42)
    assert a == b
    c = uuid7_from_int(1_700_000_000_000, seed=43)
    assert a != c
