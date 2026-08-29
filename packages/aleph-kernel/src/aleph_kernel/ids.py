"""Kernel-local id minting. stdlib only.

The kernel mints one kind of id — a `PluginId` for a dynamically registered
capability — and that was the ENTIRE reason it imported `aleph-core`. A loader
that cannot be imported without a workspace package is not a loader, and the
owner's constraint is explicit: the core depends on nothing above it.

Deliberately a COPY rather than a shared abstraction. The alternative — move
`uuid7` somewhere both can import — recreates the dependency under a new name,
because whatever holds it becomes a package the kernel needs. Fifteen lines of
bit-shuffling duplicated is the cheaper trade, and
`test_kernel_uuid7_matches_aleph_core` pins the two implementations to the same
output so the copy cannot drift into a different id format.
"""

from __future__ import annotations

import os
import time
from uuid import UUID


def uuid7() -> UUID:
    """A UUIDv7: 48-bit millisecond timestamp, then random, version and variant.

    Time-ordered on purpose — ids sort by creation, which is what makes a
    registry listing stable and a log correlatable without a separate column.
    """
    unix_ts_ms = int(time.time() * 1000)
    rand = os.urandom(10)

    ts = unix_ts_ms.to_bytes(6, "big")
    # Version 7 in the top four bits of byte 6.
    b6 = (0x70 | (rand[0] & 0x0F)).to_bytes(1, "big")
    # Variant 0b10 in the top two bits of byte 8.
    b8 = (0x80 | (rand[2] & 0x3F)).to_bytes(1, "big")
    return UUID(bytes=ts + b6 + rand[1:2] + b8 + rand[3:])
