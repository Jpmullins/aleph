"""Every route the process serves answers something other than 500. WS-P1 c5.

## What was missing

`scripts/check-project-scope.sh` proves every project-scoped route *resolves a
project*. Nothing proved any route *answers*. The route table is 128
method/path pairs and the audit found no test anywhere that enumerates
`app.routes` and drives them — `grep -rn 'app.routes' apps tests packages`
returned three hits, all in `test_catalogs_route.py`, all about one path. A
router that raises on import of a helper, a dependency that lost its provider,
a response model that no longer serialises: each of those is a 500 on one route
and green on every other test in the suite.

## Why this drives the *booted* app

`create_app()` is not the process. Half the app is assembled during lifespan:
the kernel mounts ten capabilities, `bind_to_app_state` publishes them, and the
AG-UI agent route is added *after* `create_app` returns. A smoke test over
`create_app()` alone would miss every route that reads `app.state`, which is
most of them, and would not see the agent endpoint at all. So this boots the
real lifespan against the real Postgres, Redis and asset store.

## Why the project is created over HTTP

`project_scope_dep` returns 404 for a principal who is not a member. A project
row inserted straight into the table has no membership row, so every
project-scoped route answers 404 at the dependency and the handler never runs —
125 routes "pass" without executing a line of their own code. That is not a
hypothetical: it is what the first run of this probe did. Creating the project
through `POST /v1/projects` is the production path and it is what makes the
rest of the table reachable. `test_the_handlers_were_actually_reached` is the
guard that this stays true.

## Three ways a route is driven, and why the escape hatches are small

* **Driven.** GET/DELETE as-is; POST/PUT/PATCH with `{}`. A route whose body
  model has required fields answers 422 — its dependency chain ran, its handler
  did not. A route whose body is optional executes for real, against a project
  that is empty, which is the interesting case.
* **Malformed body** (`MALFORMED_BODY`). Two routes accept `{}` and then spend
  real money: `smoke/llm` called the gateway and returned a completion during
  the probe that produced this file, and `reviews/editorial` returned 202 after
  enqueueing an arq job that an idle worker picks up. A JSON *list* cannot
  validate against an object body model, so the dependency chain still runs and
  the handler still does not.
* **Never driven** (`NEVER_DRIVEN`). Two routes take no body at all, so no
  request shape reaches them without executing them. They are named, their
  existence is asserted, and the list is capped.

Both lists are checked against the live route table, so an entry that stops
naming a real route fails the test instead of silently shrinking coverage.

## Why the whole run happens in one `asyncio.run`

Booting the kernel is ten capability setups and ten live probes. Doing that per
test function is half a minute of nothing, and a module-scoped *async* fixture
would need the event-loop scope of every test to be widened to match. A plain
module-scoped sync fixture that runs one coroutine sidesteps both: the app is
booted once, the table is driven once, and the assertions read the result.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text

if TYPE_CHECKING:
    from fastapi import FastAPI

pytestmark = pytest.mark.integration

#: Path params that are NOT UUIDs, and a value for each. Anything else is given
#: a fresh UUID and is expected to 404.
#:
#: `test_every_non_uuid_path_param_has_a_value` asserts this covers the route
#: table exactly, so a new `{kind}` segment fails loudly here instead of
#: answering 422 and quietly dropping its route out of the coverage count.
NON_UUID_PATH_PARAMS: dict[str, str] = {
    # `Literal["source", "rendered", "artifact-version"]` — a UUID is a 422.
    "asset_kind": "source",
    "tab": "wiki",
    "slug": "no-such-slug-exists",
    "anchor": "s1",
    "connector_kind": "arxiv",
}

#: Driven with a body that cannot validate, because `{}` validates and the
#: handler then spends money. The dependency chain still runs.
MALFORMED_BODY: dict[tuple[str, str], str] = {
    ("POST", "/v1/projects/{project_id}/smoke/llm"): (
        "every field defaults, so `{}` reaches the gateway — the probe that "
        "wrote this test got a 200 with a real completion in the body"
    ),
    ("POST", "/v1/projects/{project_id}/reviews/editorial"): (
        "`{}` returns 202 and enqueues an arq job; a worker then runs the "
        "reviewer against the gateway"
    ),
}

#: Not driven at all: no body, so no request shape stops at validation.
NEVER_DRIVEN: dict[tuple[str, str], str] = {
    ("POST", "/v1/projects/{project_id}/model-profile/autoconfigure"): (
        "probes every model the gateway lists, then writes the binding"
    ),
    ("POST", "/v1/projects/{project_id}/wiki/schema/propose"): (
        "derives a taxonomy from the corpus with an LLM call"
    ),
}

#: A cap on the escape hatch. Four routes out of 128 may be exempted from being
#: driven as written; the fifth has to argue for itself by changing this number.
MAX_EXEMPT = 4

#: The four SSE routes, by name. A hang is a legitimate outcome here (see
#: `test_the_streaming_routes_are_the_four_we_know_about`) and therefore has to
#: be pinned to a set, or a deadlocked route joins them unnoticed.
STREAMING_ROUTES = frozenset(
    {
        ("GET", "/v1/projects/{project_id}/agent-events/stream"),
        ("GET", "/v1/projects/{project_id}/changes/stream"),
        ("GET", "/v1/projects/{project_id}/surfaces/stream"),
        ("GET", "/v1/projects/{project_id}/surfaces/{tab}/stream"),
    }
)

#: Seconds to wait before calling a request "still streaming". `ASGITransport`
#: buffers the whole response body before returning, so an SSE route never
#: completes.
STREAM_BUDGET_S = 4.0

#: Statuses that mean the handler never ran. 422 is FastAPI's body/param
#: validator; everything else in the table (2xx, 404, 409, 204) is the handler
#: or a dependency below it answering.
#: Statuses that mean the request never reached a handler.
#:
#: 404 is in here and that is the whole point. It was not, and `project_scope_dep`
#: answers 404 for a project that does not resolve — so a fixture in which ZERO
#: handlers run scored 123 of 126 "reached", higher than the healthy 84, because
#: the real 422s were displaced by 404s from the dependency. The guard passed
#: most loudly exactly when the thing it guards was most broken.
_NOT_REACHED = frozenset({401, 403, 404, 405, 422})

#: Floor on how many driven pairs must get past validation.
#:
#: **63 of 126**, measured 2026-08-22 after `_NOT_REACHED` was corrected. The
#: previous 78 was derived from a count of 84 that included every 404 and 403 —
#: i.e. from responses produced BEFORE a handler ran. The honest number is
#: lower, and the floor sits just under it.
MIN_HANDLERS_REACHED = 58

#: Floor on the size of the route table itself, so a wiring regression that
#: unmounts half the routers cannot make this suite pass by having less to do.
MIN_ROUTE_METHODS = 120

#: Rows deleted for the throwaway project, in order. `action_ledger_events` and
#: `ledger_chain_heads` are deliberately absent: the ledger is append-only and
#: enforced by a database trigger, and a fixture that switches an invariant off
#: to tidy up is how the invariant stops being one. Same reasoning as
#: `tests/integration/conftest.py`.
_TEARDOWN = (
    "DELETE FROM assistant_threads WHERE project_id = :pid",
    "DELETE FROM assistant_sessions WHERE project_id = :pid",
    "DELETE FROM connector_bindings WHERE project_id = :pid",
    "DELETE FROM model_calls WHERE project_id = :pid",
    "DELETE FROM cost_ledger_events WHERE project_id = :pid",
    "DELETE FROM agent_runs WHERE project_id = :pid",
    "DELETE FROM wiki_schemas WHERE project_id = :pid",
    "DELETE FROM card_actions WHERE project_id = :pid",
    "DELETE FROM model_profiles WHERE project_id = :pid",
    "DELETE FROM project_members WHERE project_id = :pid",
    "DELETE FROM projects WHERE id = :pid",
)


class RouteRow(NamedTuple):
    """One method/path pair, plus what its path params need to be filled with."""

    method: str
    path: str
    #: `name -> is_uuid` for every path param except `project_id`.
    params: tuple[tuple[str, bool], ...]


class Outcome(NamedTuple):
    """What one method/path pair did."""

    method: str
    path: str
    status: int | None  #: None means it was still streaming when time ran out
    body: str


class Run(NamedTuple):
    """The result of one full sweep of the route table."""

    routes: tuple[RouteRow, ...]
    outcomes: tuple[Outcome, ...]


def _route_rows(app: FastAPI) -> list[tuple[RouteRow, APIRoute]]:
    """Every (method, template) the process serves, HEAD/OPTIONS aside."""
    out: list[tuple[RouteRow, APIRoute]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        params = tuple(
            (p.name, p.field_info.annotation is uuid.UUID)
            for p in route.dependant.path_params
            if p.name != "project_id"
        )
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((RouteRow(method, route.path, params), route))
    return sorted(out, key=lambda row: (row[0].path, row[0].method))


def _concrete_path(row: RouteRow, project_id: str) -> str:
    path = row.path.replace("{project_id}", project_id)
    for name, is_uuid in row.params:
        value = str(uuid.uuid4()) if is_uuid else NON_UUID_PATH_PARAMS[name]
        path = path.replace("{" + name + "}", value)
    return path


async def _drive(client: httpx.AsyncClient, row: RouteRow, path: str) -> Outcome:
    kwargs: dict[str, Any] = {}
    if row.method in {"POST", "PUT", "PATCH"}:
        # A JSON list cannot validate against an object body model, so the
        # request stops at the validator instead of at the gateway.
        kwargs["json"] = [] if (row.method, row.path) in MALFORMED_BODY else {}
    try:
        response = await asyncio.wait_for(
            client.request(row.method, path, **kwargs), STREAM_BUDGET_S
        )
    except TimeoutError:
        return Outcome(row.method, row.path, None, "(still streaming)")
    return Outcome(row.method, row.path, response.status_code, response.text[:400])


async def _sweep() -> Run:
    """Boot the real app, create a project over HTTP, drive every route, clean up."""
    from aleph_api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        settings = app.state.settings
        # Creating a project dispatches `bootstrap_project_job`, which is a full
        # research run. A smoke test must not start one, and there is no
        # route-level way to opt out.
        previously = getattr(settings, "bootstrap_auto_enabled", False)
        settings.bootstrap_auto_enabled = False
        rows = _route_rows(app)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        outcomes: list[Outcome] = []
        project_id = ""
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://route-smoke", timeout=60.0
            ) as client:
                created = await client.post(
                    "/v1/projects",
                    json={
                        "title": "[route-smoke] throwaway",
                        "description": "created by tests/integration/test_route_smoke.py",
                    },
                )
                assert created.status_code == 201, created.text
                project_id = str(created.json()["id"])
                for row, _route in rows:
                    if (row.method, row.path) in NEVER_DRIVEN:
                        continue
                    outcomes.append(await _drive(client, row, _concrete_path(row, project_id)))
        finally:
            settings.bootstrap_auto_enabled = previously
            if project_id:
                async with app.state.session_maker() as session:
                    for statement in _TEARDOWN:
                        await session.execute(text(statement), {"pid": uuid.UUID(project_id)})
                    await session.commit()
    return Run(tuple(row for row, _ in rows), tuple(outcomes))


@pytest.fixture(scope="module")
def run() -> Run:
    """One boot, one sweep, shared by every assertion below."""
    return asyncio.run(_sweep())


# --- the criterion ----------------------------------------------------------


def test_no_route_answers_500(run: Run) -> None:
    """THE criterion. A well-formed request to any route is not a server error.

    The body is included in the failure message because a 500's whole problem is
    that it names nothing; a test reporting only the path would repeat the
    defect it exists to catch.
    """
    failures = [o for o in run.outcomes if o.status is not None and o.status >= 500]
    assert not failures, "\n".join(
        f"{o.status} {o.method} {o.path}\n    {o.body}" for o in failures
    )


def test_the_handlers_were_actually_reached(run: Run) -> None:
    """A table of 422s would pass the test above while executing nothing.

    This is the guard on the fixture: if the project stops resolving, or a path
    param value stops matching its annotation, every affected route answers
    before its handler and the count collapses.

    "Reached" is defined by EXCLUSION and the exclusion set is the load-bearing
    part. It was `{422}` alone, and `project_scope_dep` answers **404** — so
    pointing the fixture at a nonexistent project, which stops every handler in
    the suite from running, moved this number UP from 84 to 123 and the test
    passed. A guard that scores higher the more broken its subject is, is
    pointed the wrong way round.
    """
    reached = [o for o in run.outcomes if o.status not in _NOT_REACHED]
    assert len(reached) >= MIN_HANDLERS_REACHED, (
        f"only {len(reached)} of {len(run.outcomes)} driven route-methods got "
        "past validation — the fixture is not producing a usable project, or a "
        "path param stopped matching its annotation"
    )


def test_the_streaming_routes_are_the_four_we_know_about(run: Run) -> None:
    """A hang is a pass here, so the set of hangs has to be pinned.

    `httpx.ASGITransport` buffers the entire response body before it returns, so
    an SSE route never completes and is recorded as `status=None`. That is a
    legitimate outcome — the handler got past every dependency and started
    emitting — but it is indistinguishable from a route that deadlocked. Naming
    the four is what keeps a fifth from hiding.
    """
    streaming = {(o.method, o.path) for o in run.outcomes if o.status is None}
    assert streaming == set(STREAMING_ROUTES), {
        "unexpectedly hanging": sorted(streaming - set(STREAMING_ROUTES)),
        "expected to stream and did not": sorted(set(STREAMING_ROUTES) - streaming),
    }


# --- the guards on this test's own escape hatches ---------------------------


def test_every_exemption_names_a_route_that_exists(run: Run) -> None:
    """An exemption that stops naming a route is coverage lost in silence."""
    served = {(r.method, r.path) for r in run.routes}
    named = set(NEVER_DRIVEN) | set(MALFORMED_BODY) | set(STREAMING_ROUTES)
    assert named <= served, sorted(named - served)
    assert len(NEVER_DRIVEN) + len(MALFORMED_BODY) <= MAX_EXEMPT


def test_every_non_uuid_path_param_has_a_value(run: Run) -> None:
    """The fill map covers the route table exactly — no gaps, no leftovers."""
    needed = {name for r in run.routes for name, is_uuid in r.params if not is_uuid}
    assert needed == set(NON_UUID_PATH_PARAMS), {
        "unfilled": sorted(needed - set(NON_UUID_PATH_PARAMS)),
        "stale": sorted(set(NON_UUID_PATH_PARAMS) - needed),
    }


def test_the_sweep_covered_the_whole_route_table(run: Run) -> None:
    """Nothing dropped out between enumeration and driving."""
    served = {(r.method, r.path) for r in run.routes}
    driven = {(o.method, o.path) for o in run.outcomes}
    assert driven | set(NEVER_DRIVEN) == served, sorted(served - (driven | set(NEVER_DRIVEN)))
    assert len(served) >= MIN_ROUTE_METHODS, len(served)
