"""An ordinary request produces a latency series for each stage it ran. WS-P9 c4.

## What this closes

`tests/unit/test_metrics_stage_spans.py` carries the measurement and the
decision: one `GET /v1/projects/{id}/sources` against the booted app produced
nine spans, none of them Aleph's, and — since `start_span` is the only thing
that records `aleph_stage_duration_seconds` — the busiest path in the process
fed no histogram at all. Four request-path stages now exist.

That file pins their *names*. This one pins that they are **produced by a real
request and readable from `/metrics`**, which is the half a unit test cannot
see: a span helper that raised, a meter that was never initialised, or a
histogram whose labels do not survive Prometheus rendering would all leave that
file green.

## Why `/metrics` and not `sample_value`

Reading the counter in-process would prove the recording call ran. It would not
prove the series is *exposed* — and an operator's Prometheus scrapes the
endpoint, not the process's memory. So the assertion is made against the
rendered exposition, fetched over HTTP through the same handler and the same
loopback gate a scraper meets.

## The refusal case

A request for a project the caller is not a member of answers 404 at
`project_scope_dep`, and that raise passes through `start_span`, so it is
recorded as `outcome="error"`. That is the intended reading — the scope check
*refused* — and it is asserted here because the ordering that produces it is
fragile: move the span inside the raise's handler and an hour of refusals looks
like an hour of fast successes, which is the same defect
`test_a_failing_leg_is_recorded_as_a_failure` guards on `/readyz`.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import TYPE_CHECKING, NamedTuple

import httpx
import pytest
from sqlalchemy import text

from aleph_api.middleware.auth import STAGE_AUTHENTICATE
from aleph_api.middleware.project_scope import STAGE_PROJECT_SCOPE
from aleph_observability.metrics import STAGE_DURATION

if TYPE_CHECKING:
    from fastapi import FastAPI

pytestmark = pytest.mark.integration

_TEARDOWN = (
    "DELETE FROM model_profiles WHERE project_id = :pid",
    "DELETE FROM project_members WHERE project_id = :pid",
    "DELETE FROM projects WHERE id = :pid",
)


class Run(NamedTuple):
    """One boot, two requests, and the exposition that followed them."""

    ok_status: int
    refused_status: int
    exposition: str


def _series(exposition: str, stage: str, outcome: str) -> float | None:
    """The `_count` sample for one (stage, outcome), or None if absent.

    Parsed out of the rendered text rather than read from the registry, because
    the rendered text is what a scraper gets. Label order is not assumed.
    """
    pattern = re.compile(
        rf"^{re.escape(STAGE_DURATION)}_count\{{(?P<labels>[^}}]*)\}} (?P<value>[0-9.e+-]+)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(exposition):
        labels = dict(
            re.findall(r'(\w+)="([^"]*)"', match.group("labels")),
        )
        if labels.get("stage") == stage and labels.get("outcome") == outcome:
            return float(match.group("value"))
    return None


async def _exercise() -> Run:
    from aleph_api.main import create_app

    app: FastAPI = create_app()
    async with app.router.lifespan_context(app):
        settings = app.state.settings
        previously = settings.bootstrap_auto_enabled
        settings.bootstrap_auto_enabled = False
        # Loopback, because `/metrics` refuses a non-loopback peer when no
        # ALEPH_METRICS_TOKEN is set. Same client tuple the route smoke uses.
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        project_id = ""
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://stage-metrics", timeout=60.0
            ) as client:
                created = await client.post(
                    "/v1/projects",
                    json={
                        "title": "[stage-metrics] throwaway",
                        "description": "created by tests/integration/test_route_stage_metrics.py",
                    },
                )
                assert created.status_code == 201, created.text
                project_id = str(created.json()["id"])

                served = await client.get(f"/v1/projects/{project_id}/sources")
                # A project the caller is not a member of: 404 at the scope
                # check, which is the refusal path.
                refused = await client.get(f"/v1/projects/{uuid.uuid4()}/sources")
                scrape = await client.get("/metrics")
                assert scrape.status_code == 200, scrape.text
        finally:
            settings.bootstrap_auto_enabled = previously
            if project_id:
                async with app.state.session_maker() as session:
                    for statement in _TEARDOWN:
                        await session.execute(text(statement), {"pid": uuid.UUID(project_id)})
                    await session.commit()

    return Run(served.status_code, refused.status_code, scrape.text)


@pytest.fixture(scope="module")
def run() -> Run:
    return asyncio.run(_exercise())


def test_the_requests_did_what_this_test_assumes(run: Run) -> None:
    """The guard on the fixture. If neither request reached the scope check the
    assertions below would be measuring an empty run."""
    assert run.ok_status == 200
    assert run.refused_status == 404


def test_authentication_is_a_timed_stage_in_the_exposition(run: Run) -> None:
    """Every request pays for principal resolution; now every request says so."""
    count = _series(run.exposition, STAGE_AUTHENTICATE, "ok")
    assert count is not None, (
        f"no {STAGE_DURATION}_count series with stage={STAGE_AUTHENTICATE!r} in the exposition"
    )
    # Three requests went through the middleware (create, list, refused list),
    # and `/metrics` itself is a fourth.
    assert count >= 3.0, count


def test_the_project_scope_check_is_a_timed_stage(run: Run) -> None:
    """111 of 115 project-scoped routes run it, and it was invisible."""
    count = _series(run.exposition, STAGE_PROJECT_SCOPE, "ok")
    assert count is not None, (
        f"no {STAGE_DURATION}_count series with stage={STAGE_PROJECT_SCOPE!r} in the exposition"
    )
    assert count >= 1.0, count


def test_a_refused_scope_check_is_counted_as_a_refusal(run: Run) -> None:
    """`outcome="error"` means the check refused, and it must not be filed as ok.

    A 404 raised out of `project_scope_dep` has to leave the span by the
    exception path. If a later change catches it inside the `with`, this series
    disappears and every refusal is recorded as a fast success — the exact
    defect the `/readyz` sibling test exists to prevent.
    """
    refusals = _series(run.exposition, STAGE_PROJECT_SCOPE, "error")
    assert refusals is not None, (
        "the 404 from the scope check was not recorded as an error outcome — "
        f"no stage={STAGE_PROJECT_SCOPE!r},outcome='error' series"
    )
    assert refusals >= 1.0, refusals
