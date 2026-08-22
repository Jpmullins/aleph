"""The byte counts are the real ones, on the real volumes. WS-P8 c6.

`tests/unit/test_metrics_storage.py` covers the shapes: what is published, what
is absent, what the error says. It cannot cover the thing that actually goes
wrong with a measurement, which is that it measures the wrong thing — a SQL
scalar that names a column the schema does not have, a sum over a table that
was renamed, a `statvfs` on a path nothing writes to. Every one of those passes
a stubbed test and reports a plausible number in production.

So this asserts the published numbers against the database's own answer, over a
real Postgres, and it asserts they are *nonzero*: this instance holds a 1.3 GB
database and a gigabyte of source assets, and a zero here would mean the query
is counting nothing.

**Every number here is compared with a tolerance, and that is not slack.** The
subject is a live database under concurrent write — this suite alone commits
rows while it runs — so `pg_database_size` is a different number each time it
is asked. Measured during development: two reads 40 milliseconds apart differed
by 74 KB, and an exact comparison failed one run in six. A test that needs a
retry is a defect in the test. What a tolerance of 64 MiB still catches is
every failure this test exists for: a wrong column, a wrong table, a unit
confusion, a store/measure mix-up, a zero. Those are orders of magnitude, not
kilobytes. The *exact* comparisons are reserved for what does not move — the
key set, and the vocabulary the two surfaces name it with.

The claim that `/metrics` and `/readyz` publish the *same* measurement is a
statement about the code, not about two reads of a moving number, and it is
pinned where it can be pinned exactly:
`tests/unit/test_metrics_storage.py::test_the_metrics_refresh_publishes_whatever_the_shared_measurement_returned`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pytest
from sqlalchemy import text

from aleph_api.routes.health import measure_storage, readyz
from aleph_api.routes.metrics import _refresh_pull_gauges
from aleph_observability.metrics import STORAGE_BYTES, sample_value
from aleph_observability.storage import (
    ASSET_STORED_BYTES_SQL,
    DATABASE_STORED_BYTES_SQL,
    storage_body,
)

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

pytestmark = pytest.mark.integration

#: How far apart two reads of a live, growing database may be. See the module
#: docstring: the measured drift is kilobytes and every defect is megabytes.
TOLERANCE_BYTES = 64 * 1024 * 1024


def _close(a: float, b: float) -> bool:
    return abs(a - b) < TOLERANCE_BYTES


class Measured(NamedTuple):
    series: dict[tuple[str, str], int]
    errors: dict[str, str]
    body: dict[str, Any]
    scraped: dict[tuple[str, str], float | None]
    truth: tuple[int, int]


async def _measure() -> Measured:
    """Boot the app, read the numbers two ways, and read the truth directly."""
    import json

    from aleph_api.main import create_app

    app: FastAPI = create_app()
    async with app.router.lifespan_context(app):
        request = cast("Request", type("_Request", (), {"app": app})())
        series, errors = await measure_storage(request)
        response = await readyz(request)
        body = cast("dict[str, Any]", json.loads(bytes(response.body)))
        # The `/metrics` path, not a reimplementation of it: this is the same
        # function the handler calls before rendering the exposition.
        await _refresh_pull_gauges(request)
        scraped = {key: sample_value(STORAGE_BYTES, store=key[0], measure=key[1]) for key in series}
        async with app.state.session_maker() as session:
            database = int((await session.execute(text(DATABASE_STORED_BYTES_SQL))).scalar_one())
            assets = int((await session.execute(text(ASSET_STORED_BYTES_SQL))).scalar_one())
    return Measured(series, errors, body, scraped, (database, assets))


@pytest.fixture(scope="module")
def measured() -> Measured:
    return asyncio.run(_measure())


def test_nothing_failed_to_measure(measured: Measured) -> None:
    assert measured.errors == {}


def test_the_database_size_is_the_database_size(measured: Measured) -> None:
    """Against `pg_database_size` itself, not against a fixture.

    Within 64 MiB of the direct read: the two queries are a few milliseconds
    apart and this suite writes rows between them, so an exact equality would
    be a flake. A wrong column or a wrong table is off by orders of magnitude,
    not by megabytes.
    """
    published = measured.series[("postgres", "stored")]
    assert _close(published, measured.truth[0]), (published, measured.truth[0])
    assert published > 0, "a zero here means pg_database_size counted nothing"


def test_the_asset_total_is_the_sum_of_what_was_recorded(measured: Measured) -> None:
    """The three size columns are real columns with the names the SQL uses.

    `source_assets.size_bytes` and `rendered_assets.bytes_size` disagree about
    word order, which is exactly the kind of thing a stub cannot catch: the
    query would raise `UndefinedColumn` in production and report `errors` here.
    """
    published = measured.series[("assets", "stored")]
    assert _close(published, measured.truth[1]), (published, measured.truth[1])
    assert published > 0, "a zero here means the asset sum counted nothing"


def test_the_asset_volume_is_measured(measured: Measured) -> None:
    volume = {
        measure: value
        for (store, measure), value in measured.series.items()
        if store == "assets" and measure != "stored"
    }
    assert set(volume) == {"used", "free", "total"}
    assert volume["total"] > 0
    assert volume["used"] + volume["free"] <= volume["total"]


def test_readyz_publishes_the_same_numbers(measured: Measured) -> None:
    """One measurement, two surfaces, one vocabulary.

    The KEYS are compared exactly — that is the part that cannot drift on its
    own — and the numbers within tolerance, for the reason in the module
    docstring.
    """
    storage = measured.body["storage"]
    expected = storage_body(measured.series)
    assert {store: sorted(fields) for store, fields in storage.items()} == {
        store: sorted(fields) for store, fields in expected.items()
    }
    for store, fields in expected.items():
        for field, value in fields.items():
            assert _close(storage[store][field], value), (
                store,
                field,
                storage[store][field],
                value,
            )
    assert "errors" not in storage


def test_the_metrics_scrape_reads_the_real_database(measured: Measured) -> None:
    """`/metrics` reaches the gauge through its own refresh, not through a stub.

    `_refresh_pull_gauges` is what the handler calls one line before rendering,
    so a storage read wired only into `/readyz` — the shape this whole repo keeps
    finding, a producer with one consumer — leaves these series unset and this
    fails on `None`.

    That the two surfaces publish the *same* measurement is a claim about the
    code and is pinned exactly, without a live database, in
    `tests/unit/test_metrics_storage.py`.
    """
    for key in (("postgres", "stored"), ("assets", "stored"), ("assets", "total")):
        value = measured.scraped[key]
        assert value is not None, f"{key} never reached the exposition"
        assert value > 0, f"{key} reached the exposition as {value}"
        assert _close(value, measured.series[key]), (key, value, measured.series[key])


def test_the_storage_section_is_not_in_the_verdict(measured: Measured) -> None:
    """A full disk is a warning, not a reason to fail the container healthcheck."""
    assert measured.body["verdict_over"] == ["postgres", "redis", "asset_store"]
