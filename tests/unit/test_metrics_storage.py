"""Bytes on the two volumes are visible, and an unread number is not a zero.

WS-P8 c6. Before this, `/metrics` carried nine `aleph_` families and not one of
them was a byte count, and `/readyz` reported a boolean per dependency and no
size at all — so the whole dataset sat on `postgres-data` and `assets` with
nothing watching either. "The disk filled up" was learnable only by dying.

The property that needs a test more than the happy path is the *failure* shape.
A gauge that reports 0 when the probe errored is worse than no gauge: an alert
on "free bytes below X" fires permanently the first time the asset root is
unreadable, and a dashboard showing a flat zero line reads as "empty volume",
not as "nobody measured". So the assertions below are mostly about absence:
what is missing from the mapping, what is missing from the exposition, and what
the error says instead.

The real numbers against the real Postgres are in
`tests/integration/test_route_storage_bytes.py`; these are the pure and stubbed
halves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Request

from aleph_api.routes.health import measure_storage, readyz
from aleph_api.routes.metrics import _refresh_pull_gauges
from aleph_observability.metrics import (
    STORAGE_BYTES,
    init_metrics,
    render_prometheus,
    replace_storage_bytes,
    sample_value,
)
from aleph_observability.storage import (
    MEASURES,
    STORES,
    VolumeUsage,
    storage_body,
    storage_series,
    volume_usage,
)

# --- stubs, shaped like `apps/api/tests/unit/test_readyz.py` ----------------


class _Result:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _Session:
    """Answers by looking at the statement, not by call order.

    `_postgres` issues `SELECT 1` on the same session factory, so a stub that
    popped from a list would hand the readiness probe the database size and
    then run out. Reading the SQL keeps the two probes independent, which is
    what the route relies on.
    """

    def __init__(self, database: int | None, assets: int | None) -> None:
        self._database = database
        self._assets = assets

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, statement: object = None) -> _Result:
        sql = str(statement)
        if "pg_database_size" in sql:
            if self._database is None:
                msg = "pg_database_size is not available"
                raise RuntimeError(msg)
            return _Result(self._database)
        if "source_assets" in sql:
            if self._assets is None:
                msg = "the asset tables are not available"
                raise RuntimeError(msg)
            return _Result(self._assets)
        return _Result(1)


class _Maker:
    def __init__(self, database: int | None = 1, assets: int | None = 1) -> None:
        self._database = database
        self._assets = assets

    def __call__(self) -> _Session:
        return _Session(self._database, self._assets)


class _Redis:
    async def ping(self) -> bool:
        return True


class _StoredAsset:
    storage_uri = "file://probe"


class _AssetStore:
    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> _StoredAsset:
        del key, data, mime_type
        return _StoredAsset()

    def get(self, storage_uri: str) -> bytes:
        del storage_uri
        return b"ok"


class _Litellm:
    async def health(self) -> bool:
        return True


class _Settings:
    def __init__(self, backend: str, root: str) -> None:
        self.aleph_asset_backend = backend
        self.aleph_asset_root = root


class _State:
    def __init__(self, maker: Any, settings: Any) -> None:
        self.session_maker = maker
        self.redis = _Redis()
        self.asset_store = _AssetStore()
        self.litellm = _Litellm()
        self.settings = settings


class _App:
    def __init__(self, maker: Any, settings: Any) -> None:
        self.state = _State(maker, settings)


def _request(*, maker: Any, settings: Any) -> Request:
    return cast("Request", type("_Request", (), {"app": _App(maker, settings)})())


async def _body(response: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(bytes(response.body)))


# --- the pure half ----------------------------------------------------------


def test_volume_usage_reads_a_real_filesystem(tmp_path: Path) -> None:
    """One `statvfs`, and the three numbers agree with each other."""
    usage = volume_usage(tmp_path)
    assert usage.total_bytes > 0
    assert usage.free_bytes >= 0
    assert usage.used_bytes + usage.free_bytes <= usage.total_bytes


def test_volume_usage_raises_rather_than_reporting_zero(tmp_path: Path) -> None:
    """A missing asset root is a fault, not an empty volume.

    Returning 0 here would publish `free_bytes 0` for a path that does not
    exist, which is the exact reading an operator would page on.
    """
    with pytest.raises(OSError):
        volume_usage(tmp_path / "does-not-exist")


def test_a_number_that_could_not_be_read_is_absent_not_zero() -> None:
    series = storage_series(database_stored_bytes=None, asset_stored_bytes=7)
    assert series == {("assets", "stored"): 7}
    assert ("postgres", "stored") not in series


def test_the_series_uses_only_the_declared_vocabulary() -> None:
    """`store` and `measure` are bounded by literals, so the family is bounded.

    Eight series is the ceiling. The label-cardinality rule in
    `aleph_observability.metrics` says the answer to "how many distinct values"
    has to be a number; this is that number.
    """
    series = storage_series(
        database_stored_bytes=1,
        asset_stored_bytes=2,
        asset_volume=VolumeUsage(total_bytes=30, used_bytes=10, free_bytes=20),
    )
    assert {store for store, _ in series} <= set(STORES)
    assert {measure for _, measure in series} <= set(MEASURES)
    assert len(STORES) * len(MEASURES) == 8


def test_the_body_is_derived_from_the_series() -> None:
    """One measurement, two shapes — so the two surfaces cannot disagree."""
    series = storage_series(
        database_stored_bytes=11,
        asset_stored_bytes=22,
        asset_volume=VolumeUsage(total_bytes=300, used_bytes=100, free_bytes=200),
    )
    assert storage_body(series) == {
        "postgres": {"stored_bytes": 11},
        "assets": {
            "stored_bytes": 22,
            "used_bytes": 100,
            "free_bytes": 200,
            "total_bytes": 300,
        },
    }


# --- the exposition ---------------------------------------------------------


def test_the_gauge_reaches_the_prometheus_exposition() -> None:
    init_metrics()
    replace_storage_bytes({("postgres", "stored"): 4321, ("assets", "free"): 99})
    payload, _ = render_prometheus()
    lines = [line for line in payload.decode().splitlines() if STORAGE_BYTES in line]
    assert f'{STORAGE_BYTES}{{measure="stored",store="postgres"}} 4321.0' in lines, lines
    assert f'{STORAGE_BYTES}{{measure="free",store="assets"}} 99.0' in lines, lines
    assert sample_value(STORAGE_BYTES, store="postgres", measure="stored") == 4321
    assert sample_value(STORAGE_BYTES, store="assets", measure="free") == 99


def test_a_store_that_stops_being_measurable_stops_being_reported() -> None:
    """Whole-family replace, for the reason the other pull gauges have it.

    A byte gauge frozen at its last reading is how "the volume is 40% full"
    survives the volume filling up.
    """
    init_metrics()
    replace_storage_bytes({("postgres", "stored"): 1, ("assets", "free"): 2})
    assert sample_value(STORAGE_BYTES, store="assets", measure="free") == 2
    replace_storage_bytes({("postgres", "stored"): 1})
    assert sample_value(STORAGE_BYTES, store="assets", measure="free") is None


async def test_the_metrics_refresh_publishes_whatever_the_shared_measurement_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`/metrics` and `/readyz` are the same measurement, pinned exactly.

    The integration test cannot assert this: `pg_database_size` is a live,
    growing number, so two reads of it disagree by kilobytes and an exact
    comparison there is a flake (measured: one run in six). The claim being made
    is about the CODE — that `_refresh_pull_gauges` publishes
    `health.measure_storage`'s answer rather than a second implementation of it
    — and that is exactly assertable by making the shared function return a
    value no database would ever produce and looking for it in the exposition.

    `_refresh_pull_gauges` is what the handler calls one line before rendering,
    so this is the real path, not a reimplementation of it.
    """
    sentinel = {("postgres", "stored"): 424242, ("assets", "free"): 131313}

    async def _fake(_request: Any) -> tuple[dict[tuple[str, str], int], dict[str, str]]:
        return sentinel, {}

    monkeypatch.setattr("aleph_api.routes.metrics.measure_storage", _fake)
    init_metrics()

    await _refresh_pull_gauges(_request(maker=_Maker(), settings=_Settings("fs", str(tmp_path))))

    assert sample_value(STORAGE_BYTES, store="postgres", measure="stored") == 424242
    assert sample_value(STORAGE_BYTES, store="assets", measure="free") == 131313


# --- the route --------------------------------------------------------------


async def test_readyz_reports_bytes_for_both_volumes(tmp_path: Path) -> None:
    request = _request(
        maker=_Maker(database=1_359_238_835, assets=1_020_806_927),
        settings=_Settings("fs", str(tmp_path)),
    )
    body = await _body(await readyz(request))
    storage = body["storage"]
    assert storage["postgres"]["stored_bytes"] == 1_359_238_835
    assert storage["assets"]["stored_bytes"] == 1_020_806_927
    assert storage["assets"]["total_bytes"] > 0
    assert "errors" not in storage


async def test_the_storage_section_does_not_vote(tmp_path: Path) -> None:
    """A volume that cannot be measured is not a reason to restart a container.

    `/readyz` is the compose healthcheck and `up -d --wait` blocks on it. If a
    byte read could fail the verdict, an unreadable asset root would stop the
    entire stack from coming up — the exact outage shape the gateway leg was
    taken out of the verdict to prevent.
    """
    request = _request(
        maker=_Maker(database=None, assets=None),
        settings=_Settings("fs", str(tmp_path / "gone")),
    )
    response = await readyz(request)
    body = await _body(response)

    assert response.status_code == 200
    assert body["status"] == "ready"
    assert "storage" not in body["verdict_over"]
    # and the failure is still legible rather than silently absent
    assert set(body["storage"]["errors"]) == {"stored", "asset_volume"}


async def test_an_unreadable_volume_names_the_reason_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    request = _request(
        maker=_Maker(database=5, assets=6),
        settings=_Settings("fs", str(tmp_path / "gone")),
    )
    body = await _body(await readyz(request))
    storage = body["storage"]
    assert storage["postgres"]["stored_bytes"] == 5
    assert "used_bytes" not in storage["assets"]
    assert "FileNotFoundError" in storage["errors"]["asset_volume"]


async def test_an_s3_backend_publishes_no_volume_series(tmp_path: Path) -> None:
    """There is no local filesystem to measure, and no error either.

    Publishing the API container's own root disk under the asset store's name
    would be a number that looks right and means nothing.
    """
    series, errors = await measure_storage(
        _request(
            maker=_Maker(database=7, assets=8),
            settings=_Settings("s3", str(tmp_path)),
        )
    )
    assert series == {("postgres", "stored"): 7, ("assets", "stored"): 8}
    assert errors == {}


async def test_a_dead_database_leaves_both_stored_numbers_absent(
    tmp_path: Path,
) -> None:
    def _boom() -> Any:
        msg = "no pool"
        raise RuntimeError(msg)

    series, errors = await measure_storage(
        _request(maker=_boom, settings=_Settings("fs", str(tmp_path)))
    )
    assert ("postgres", "stored") not in series
    assert ("assets", "stored") not in series
    assert ("assets", "total") in series, "one failure must not cost the other read"
    assert "no pool" in errors["stored"]
