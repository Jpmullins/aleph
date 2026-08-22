"""How many bytes the dataset occupies, and how many are left. WS-P8 c6.

Aleph's whole state lives on two docker volumes — `postgres-data` mounted at
`/var/lib/postgresql/data`, and `assets` mounted at `/app/data/assets`
(`deploy/compose/docker-compose.yml`). Until this module existed neither was
measured anywhere: `/metrics` carried nine `aleph_` families and not one of
them was a byte count, and `/readyz` reported a boolean per dependency and no
size at all. "The disk filled up" was therefore a thing Aleph could only learn
by dying, and "the corpus doubled last week" was not answerable at all.

## The vocabulary, and why `stored` is not `used`

    store    postgres | assets
    measure  stored | used | free | total

`stored` is what Aleph knows it wrote: `pg_database_size(current_database())`
for the database, and the sum of the recorded byte sizes for the asset store.
`used`, `free` and `total` are `statvfs` on the filesystem the store sits on.

They are deliberately different words because they are different numbers, and
conflating them is how a dashboard lies. Under compose the asset volume is a
*named* docker volume, so `used` and `free` describe the whole docker
filesystem — shared with the postgres volume, the images, and everything else
on that host. `stored` is the only number that is about Aleph's assets alone,
and `free` is the only one that answers "when does this stop working".

## Why the database has no `free`

Postgres exposes its own size and nothing about the filesystem underneath it —
there is no `statvfs` equivalent in SQL, and the API process cannot see
`/var/lib/postgresql/data`, which is inside another container. So
`{store="postgres"}` carries `stored` only. Reporting a `free` for it would
mean reporting the *API's* filesystem under the database's name, which is worse
than reporting nothing.

## Cost

`pg_database_size` stats every file in the database directory. Measured against
this instance's 1.3 GB database: **1.0 ms**. `shutil.disk_usage` is a single
`statvfs`. Both are cheap enough to run on the `/readyz` path, which compose
calls every 15 seconds inside a 4-second client budget — so neither is cached,
and neither can go stale.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "ASSET_STORED_BYTES_SQL",
    "DATABASE_STORED_BYTES_SQL",
    "MEASURES",
    "STORES",
    "VolumeUsage",
    "storage_body",
    "storage_series",
    "volume_usage",
]

#: The `store` label's complete value set. Bounded by this literal, not by data.
STORES: Final = ("postgres", "assets")

#: The `measure` label's complete value set. See the module docstring for why
#: `stored` and `used` are separate words.
MEASURES: Final = ("stored", "used", "free", "total")

#: Bytes the database occupies on its volume.
DATABASE_STORED_BYTES_SQL: Final = "SELECT pg_database_size(current_database())"

#: Bytes the asset store holds, summed from what each write recorded.
#:
#: Three tables and two different column names (`size_bytes` on `source_assets`,
#: `bytes_size` on the other two) because that is what the schema says; the
#: mismatch is older than this module and renaming a column is a migration, not
#: a metrics change.
#:
#: This counts what Aleph *believes* it stored. It will disagree with the
#: filesystem if a write recorded a row and lost its bytes, which is exactly the
#: kind of divergence worth being able to see — so it is published beside the
#: volume's own `used`, not instead of it.
ASSET_STORED_BYTES_SQL: Final = (
    "SELECT (SELECT COALESCE(SUM(size_bytes), 0) FROM source_assets)"
    " + (SELECT COALESCE(SUM(bytes_size), 0) FROM rendered_assets)"
    " + (SELECT COALESCE(SUM(bytes_size), 0) FROM artifact_versions)"
)


@dataclass(frozen=True)
class VolumeUsage:
    """`statvfs` on one filesystem, in bytes."""

    total_bytes: int
    used_bytes: int
    free_bytes: int


def volume_usage(path: str | Path) -> VolumeUsage:
    """Total, used and free bytes of the filesystem holding `path`.

    Raises whatever the OS raises — a missing asset root is a real fault and the
    caller reports it as one, rather than publishing a zero that is
    indistinguishable from an empty volume.
    """
    usage = shutil.disk_usage(Path(path))
    return VolumeUsage(
        total_bytes=int(usage.total),
        used_bytes=int(usage.used),
        free_bytes=int(usage.free),
    )


def storage_series(
    *,
    database_stored_bytes: int | None = None,
    asset_stored_bytes: int | None = None,
    asset_volume: VolumeUsage | None = None,
) -> dict[tuple[str, str], int]:
    """`(store, measure) -> bytes`, for whatever could be measured.

    A number that could not be read is absent rather than zero. A gauge reading
    zero because nobody measured is the failure `scripts/status.sh` prints `n/a`
    to avoid, and it is worse here: an alert on "free bytes below X" fires
    permanently the first time the probe errors.
    """
    series: dict[tuple[str, str], int] = {}
    if database_stored_bytes is not None:
        series[("postgres", "stored")] = int(database_stored_bytes)
    if asset_stored_bytes is not None:
        series[("assets", "stored")] = int(asset_stored_bytes)
    if asset_volume is not None:
        series[("assets", "used")] = asset_volume.used_bytes
        series[("assets", "free")] = asset_volume.free_bytes
        series[("assets", "total")] = asset_volume.total_bytes
    return series


def storage_body(series: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    """The same numbers, nested for a JSON body.

    `/readyz` and `/metrics` publish one measurement in two shapes; deriving the
    body from the series is what stops the two surfaces disagreeing about what a
    word means.
    """
    body: dict[str, dict[str, int]] = {}
    for (store, measure), value in series.items():
        body.setdefault(store, {})[f"{measure}_bytes"] = value
    return body
