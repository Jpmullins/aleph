"""`GET /v1/projects/{id}/catalogs` is actually mounted. WS-A3b.

A router module that nothing includes is the dominant defect class in this
codebase written down as a file: correct code, no caller, and every test of its
internals green. `tests/integration/test_catalog_capability.py` exercises the
route's helpers against a real database and would pass just as happily if
`main.py` never mentioned the router — so the reachability of the endpoint needs
its own assertion, and it needs to be made against the app the process actually
builds.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from aleph_api.main import create_app

CATALOGS_PATH = "/v1/projects/{project_id}/catalogs"


def test_the_catalogs_route_is_reachable_on_the_real_app() -> None:
    app = create_app()
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert CATALOGS_PATH in paths


def test_it_is_a_GET_and_nothing_else() -> None:
    """A catalog listing is a read. Anything that mutates the set belongs on the
    plugin install path, where the AST gate and the ledger are.
    """
    app = create_app()
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == CATALOGS_PATH)
    assert route.methods == {"GET"}


def test_it_resolves_the_project_rather_than_trusting_the_url() -> None:
    """`ProjectScopeDep`, not a bare UUID.

    A route that names a project and checks nothing is an existence oracle for
    any UUID a caller can guess — and this one lists another tenant's installed
    plugins by name. `scripts/check-project-scope.sh` sweeps for this across all
    115 project-scoped routes; asserted here too so the failure names the route
    rather than a count.
    """
    app = create_app()
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == CATALOGS_PATH)
    names = {d.call.__name__ for d in route.dependant.dependencies if d.call is not None}
    assert "project_scope_dep" in names, names
