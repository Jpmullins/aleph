"""UUIDv7 generation. Time-ordered, ULID-shaped, safe in Postgres b-tree indexes.

UUIDv7 layout (per RFC 9562):
    field         bits   notes
    unix_ts_ms    48     milliseconds since Unix epoch
    ver           4      0b0111
    rand_a        12     random
    var           2      0b10
    rand_b        62     random
"""

from __future__ import annotations

import os
import time
from uuid import UUID


def _build(unix_ts_ms: int, rand_bytes: bytes) -> UUID:
    if not 0 <= unix_ts_ms < (1 << 48):
        msg = f"unix_ts_ms out of range: {unix_ts_ms}"
        raise ValueError(msg)
    if len(rand_bytes) != 10:
        msg = f"rand_bytes must be 10 bytes, got {len(rand_bytes)}"
        raise ValueError(msg)

    # 6 bytes timestamp + 10 bytes random/version/variant
    ts = unix_ts_ms.to_bytes(6, "big")

    # Insert version 7 in the top 4 bits of byte 6 (index 0 of rand_bytes).
    b6 = (0x70 | (rand_bytes[0] & 0x0F)).to_bytes(1, "big")
    # Insert variant 10 in the top 2 bits of byte 8 (index 2 of rand_bytes).
    b8 = (0x80 | (rand_bytes[2] & 0x3F)).to_bytes(1, "big")

    raw = ts + b6 + rand_bytes[1:2] + b8 + rand_bytes[3:]
    return UUID(bytes=raw)


def uuid7() -> UUID:
    """Generate a new UUIDv7 from current time + os.urandom."""
    return _build(int(time.time() * 1000), os.urandom(10))


def uuid7_from_int(unix_ts_ms: int, *, seed: int = 0) -> UUID:
    """Deterministic UUIDv7 for tests. Same (unix_ts_ms, seed) → same UUID."""
    # Reproducible "random" bytes from seed.
    rng = seed.to_bytes(8, "big", signed=False) + b"\x00\x00"
    return _build(unix_ts_ms, rng[:10])
