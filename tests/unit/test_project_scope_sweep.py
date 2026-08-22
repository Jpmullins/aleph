"""The project-scope sweep must model reality, not merely run.

`scripts/check-project-scope.sh` guards the whole tenant boundary, so it is
worth exactly as much as its false-positive and false-negative rates. The
surface-bindings sweep earned itself a dedicated test file by reporting two
false positives on its first run — `GroundingSurface.claim` and `.groundings`,
declared with `z3.any()` rather than `CommonSchemas.DynamicValue` — and the
reaction to a noisy sweep is to switch it off, not to fix it.

These pin both directions on the four cases that matter:

  * a scoped handler is quiet;
  * an unscoped handler is reported, with its file, line and route template;
  * an allowlisted handler is quiet, and is still counted as an exemption;
  * a *non-route* function with a `project_id` parameter is not a route.

The last one is the live false-positive class: `apps/api/.../routes/` holds ~20
private helpers with signatures like `_build_tab_messages(session, project_id,
tab_lc)` and `_load_draft_page(project_id, page_id, session)`. A grep for
`project_id: UUID` reports every one of them.
"""

from __future__ import annotations

from project_scope import offenders, project_scoped_routes

SCOPED = """
from aleph_api.middleware.project_scope import ProjectScopeDep

router = APIRouter(prefix="/v1/projects", tags=["notes"])


@router.get("/{project_id}/notes")
async def get_notes(project_id: ProjectScopeDep, session: SessionDep) -> list[NoteOut]:
    return []
"""

UNSCOPED = """
router = APIRouter(prefix="/v1/projects", tags=["notes"])


@router.get("/{project_id}/notes")
async def get_notes(project_id: UUID, session: SessionDep) -> list[NoteOut]:
    return []
"""


def test_a_scoped_handler_is_quiet() -> None:
    routes = project_scoped_routes(SCOPED, "routes/notes.py")
    assert [r.scoped_by for r in routes] == ["ProjectScopeDep"]
    assert offenders(routes, {}) == []


def test_an_unscoped_handler_is_reported_with_file_and_line() -> None:
    """The exact defect: a path parameter that resolves no membership."""
    found = offenders(project_scoped_routes(UNSCOPED, "routes/notes.py"), {})
    assert len(found) == 1
    (route,) = found
    assert route.file == "routes/notes.py"
    assert route.function == "get_notes"
    assert route.path == "/v1/projects/{project_id}/notes"
    # The line must point at the handler, not at the module: an offender you
    # have to go and find is an offender that gets an allowlist entry instead
    # of a fix.
    assert UNSCOPED.splitlines()[route.line - 1].startswith("async def get_notes")


def test_an_allowlisted_handler_is_quiet_but_still_a_route() -> None:
    routes = project_scoped_routes(UNSCOPED, "routes/notes.py")
    allowlist = {"routes/notes.py::get_notes": "reason recorded here"}
    assert offenders(routes, allowlist) == []
    # Still classified as unscoped — the exemption suppresses the failure, it
    # does not rewrite the finding. `scan()` counts these separately and prints
    # them on every green run so the list cannot grow unread.
    assert routes[0].scoped_by is None


def test_a_non_route_function_named_project_id_is_not_a_route() -> None:
    """The false-positive class a grep cannot avoid.

    `_build_tab_messages(session, project_id, tab_lc)` and its ~20 siblings in
    `routes/` take a `project_id: UUID` and are not routes. Flagging them would
    make the sweep 95% noise on its first run.
    """
    source = """
router = APIRouter(prefix="/v1/projects")


async def _build_tab_messages(session: Any, project_id: UUID, tab_lc: str) -> list[Any]:
    return []


def _retracted_page_ids(session: Any, project_id: UUID) -> set[UUID]:
    return set()
"""
    assert project_scoped_routes(source, "routes/surfaces.py") == []


def test_a_route_with_no_project_in_its_path_is_not_this_sweeps_business() -> None:
    """`GET /v1/projects` (list) and `POST /v1/projects` (create) scope nothing.

    They must not: there is no project yet to be a member of. The sweep keys on
    the `{project_id}` path parameter, never on the router prefix.
    """
    source = """
router = APIRouter(prefix="/v1/projects")


@router.post("")
async def create_project(body: ProjectIn, principal: PrincipalDep) -> ProjectOut:
    ...
"""
    assert project_scoped_routes(source, "routes/projects.py") == []


def test_the_project_id_can_come_from_the_router_prefix() -> None:
    """A prefix carrying `{project_id}` makes every route on it project-scoped.

    Nothing spells it this way today. That is the reason to handle it: the first
    router that does would otherwise be exempt from the sweep by accident, and
    an accidental exemption is silent.
    """
    source = """
router = APIRouter(prefix="/v1/projects/{project_id}/plugins")


@router.get("")
async def list_plugins(project_id: UUID) -> list[str]:
    return []
"""
    routes = project_scoped_routes(source, "routes/plugins.py")
    assert [r.path for r in routes] == ["/v1/projects/{project_id}/plugins"]
    assert offenders(routes, {})


def test_an_sse_route_guarded_by_assert_stream_access_is_scoped() -> None:
    """SSE routes cannot take ProjectScopeDep and are not offenders.

    `ProjectScopeDep` pulls in the request-scoped `SessionDep`, which stays
    checked out until the request ends. An SSE request never ends, so every open
    stream would hold one of the 30 pool connections for its lifetime and
    switching tabs would exhaust the pool.
    """
    source = """
router = APIRouter(prefix="/v1/projects")


@router.get("/{project_id}/changes/stream", response_model=None)
async def stream_changes(
    project_id: Annotated[UUID, Path(...)],
    request: Request,
    principal: PrincipalDep,
) -> StreamingResponse:
    await assert_stream_access(request, project_id, principal)
    return StreamingResponse(gen())
"""
    routes = project_scoped_routes(source, "routes/changes.py")
    assert [r.scoped_by for r in routes] == ["assert_stream_access"]
    assert offenders(routes, {}) == []


def test_a_stream_guard_on_some_other_id_does_not_scope_this_route() -> None:
    """ "It calls the guard somewhere" is not "this project is checked".

    The page-lock sweep was defeated once by exactly this depth of matching:
    it saw `with_for_update` and never looked at `read=True`. Here the analogue
    is scope-checking an id the route did not receive from its own path.
    """
    source = """
router = APIRouter(prefix="/v1/projects")


@router.get("/{project_id}/changes/stream", response_model=None)
async def stream_changes(
    project_id: Annotated[UUID, Path(...)],
    request: Request,
    principal: PrincipalDep,
    other_project_id: UUID = Query(...),
) -> StreamingResponse:
    await assert_stream_access(request, other_project_id, principal)
    return StreamingResponse(gen())
"""
    routes = project_scoped_routes(source, "routes/changes.py")
    assert [r.scoped_by for r in routes] == [None]
    assert offenders(routes, {})


def test_a_decorator_level_dependency_scopes_the_route() -> None:
    """The idiomatic FastAPI spelling, accepted so it is never a false positive."""
    source = """
router = APIRouter(prefix="/v1/projects")


@router.post("/{project_id}/notes", dependencies=[Depends(project_scope_dep)])
async def add_note(project_id: UUID, body: NoteIn) -> NoteOut:
    ...
"""
    routes = project_scoped_routes(source, "routes/notes.py")
    assert [r.scoped_by for r in routes] == ["dependencies="]
    assert offenders(routes, {}) == []


def test_a_router_level_dependency_scopes_every_route_on_it() -> None:
    source = """
router = APIRouter(prefix="/v1/projects", dependencies=[Depends(project_scope_dep)])


@router.get("/{project_id}/notes")
async def get_notes(project_id: UUID) -> list[NoteOut]:
    return []
"""
    routes = project_scoped_routes(source, "routes/notes.py")
    assert [r.scoped_by for r in routes] == ["APIRouter(dependencies=)"]
    assert offenders(routes, {}) == []


def test_a_dot_get_on_something_that_is_not_a_router_is_not_a_route() -> None:
    """`payload.get("project_id")` and `client.get(url)` are not route decorators.

    The decorator must be an attribute call on a name this module bound to an
    `APIRouter`, which is why the prefix table is built first.
    """
    source = """
router = APIRouter(prefix="/v1/projects")

cache = {}


@cache.get("/{project_id}/notes")
def not_a_route(project_id: UUID) -> None:
    ...
"""
    assert project_scoped_routes(source, "routes/notes.py") == []
