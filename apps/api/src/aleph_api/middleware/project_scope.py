"""Project-scope helper: resolves and caches a Principal's role for a project_id.

This is a *dependency* (not an ASGI middleware) because it needs the FastAPI
path param. Use `project_scope_dep(project_id)` as a route dependency.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path, Request

from aleph_api.deps import PrincipalDep, SessionDep
from aleph_core.errors import Conflict, NotFound
from aleph_db.repos.project import get_member_role_and_status
from aleph_observability.tracing import start_span
from aleph_security.principal import Principal

#: HTTP methods that only read. Everything else mutates.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Stage names for the request-path spans in this module. See
#: `aleph_api.middleware.auth` for why these are constants and not inline
#: strings, and `tests/unit/test_metrics_stage_spans.py` for the sweep that
#: enforces it.
STAGE_PROJECT_SCOPE = "api.project_scope"
STAGE_STREAM_ACCESS = "api.stream_access"

#: Statuses that accept writes. `archived` is deliberately read-only — that is
#: what archiving is for — and `deleted` accepts nothing but its own restore.
_WRITABLE_STATUSES = frozenset({"active"})


#: The one route that can bring a project back.
#:
#: Matched as a *route template*, never by formatting the id into the URL and
#: comparing strings. The first version of this did exactly that, and it was a
#: permanent lockout: `request.url.path` carries whatever UUID spelling the
#: client sent, while an f-string of a `UUID` is always canonical lowercase. A
#: caller using uppercase hex — which `uuid.UUID()` accepts and FastAPI parses
#: happily — failed the comparison and got a 409 telling them to perform the
#: request that had just been refused. Verified against the running server:
#: lowercase restore 200, uppercase restore 409.
_RESTORE_ROUTE = "/v1/projects/{project_id}"


def _is_project_status_change(request: Request) -> bool:
    """True for the one request that can bring a project back: its own PATCH.

    Without this exemption the rule below is a trap door. `PATCH
    /v1/projects/{id}` with `{"status": "active"}` is the *only* writer of
    `Project.status` in the codebase — delete and restore are the same request
    with a different body — so exempting that one route template exempts restore
    by construction. There is no second door to keep open, and none to forget.

    The body is deliberately not inspected. `ProjectUpdate` is `extra="forbid"`
    with only `title`, `description` and `status`, so the blast radius of the
    exemption is three scalar columns; reading the body inside a dependency that
    runs on 116 handlers would buy nothing and add a body-consumption failure
    mode to all of them.
    """
    route = request.scope.get("route")
    return request.method == "PATCH" and getattr(route, "path", None) == _RESTORE_ROUTE


#: Routes that are POSTs and are not writes.
#:
#: Exporting a vault produces a zip, which is why it cannot be a GET — but it
#: reads the wiki and changes nothing. Blocking it on an archived project locks
#: a person out of the one thing an exit hatch exists for: taking their
#: knowledge with them from a project they have already archived. Reads stay
#: open on an archived project for exactly this reason; this route was on the
#: wrong side of the method check, not the wrong side of the rule.
#:
#: Kept as an explicit list, not a naming convention: "any route with 'export'
#: in it is safe" is the kind of rule that admits the first route somebody names
#: `export_and_purge`.
_NON_MUTATING_POSTS: frozenset[str] = frozenset({"/v1/projects/{project_id}/export/vault"})


def _assert_project_writable(request: Request, project_id: UUID, status: str) -> None:
    """Refuse writes to a project that is archived or deleted.

    A soft-deleted project used to accept every write. The project *list*
    filters on status; the write paths ignored it entirely. The result, observed
    in production: a project was deleted at 15:09, and at 17:22 a research run
    ingested 20 papers into it, computed embeddings, and built 223 wiki pages.
    Every step reported success. The work was simply unreachable afterwards,
    because nothing surfaces a deleted project.

    That is the house failure mode — the operation succeeds, the state it writes
    is orphaned, and nothing errors.

    Reads stay open on purpose: a member must be able to inspect a project
    before deciding to restore it, and the list already hides it. `Conflict`
    (409) rather than 404 or 403 for the same reason — the project exists and
    you may access it; it is the project's *state* that forbids the write, and
    saying so is what tells a user to restore it. A 404 would hide the thing
    they need to find.
    """
    if request.method in _READ_METHODS:
        return
    route_path = getattr(request.scope.get("route"), "path", None)
    if route_path in _NON_MUTATING_POSTS:
        return
    if status in _WRITABLE_STATUSES:
        return
    if _is_project_status_change(request):
        return
    msg = (
        f"project is {status} and accepts no writes; restore it first "
        f"(PATCH /v1/projects/{project_id} with status=active)"
    )
    raise Conflict(msg)


def _assert_credential_scope(principal: Principal, project_id: UUID) -> None:
    """Refuse a credential that is bound to a *different* project.

    Agent tokens carry a signed `project_id` and are minted behind an OWNER
    gate on that project. Until this check existed the middleware dropped the
    claim, so any agent token authorized every project its underlying user
    belonged to for the token's full lifetime.

    Raises `NotFound` rather than `PermissionDenied` to match the surrounding
    policy: existence must not leak across project scopes. Runs before the
    membership query so a mis-scoped credential costs no I/O.
    """
    if principal.project_id is not None and principal.project_id != project_id:
        msg = f"project not found: {project_id}"
        raise NotFound(msg)


async def project_scope_dep(
    project_id: Annotated[UUID, Path(...)],
    principal: PrincipalDep,
    session: SessionDep,
    request: Request,
) -> UUID:
    """Resolve membership, refuse writes to a non-active project, cache the role.

    Returns 404 (NotFound) if the principal is not a member of the
    project — never leak existence across project scopes. Returns 409 (Conflict)
    if they are a member but the project is archived or deleted and the request
    would write.
    """
    # 111 of the 115 project-scoped routes run this, so it is the single
    # busiest piece of Aleph's own code on the request path — and it was
    # invisible: the SQLAlchemy instrumentation names its query `SELECT`, same
    # as the handler's. The span separates "the scope check was slow" from "the
    # handler was slow", and `aleph_stage_duration_seconds{stage=
    # "api.project_scope"}` makes the first one alertable.
    with start_span(STAGE_PROJECT_SCOPE, **{"aleph.write": request.method not in _READ_METHODS}):
        _assert_credential_scope(principal, project_id)
        found = await get_member_role_and_status(
            session, project_id=project_id, user_id=principal.user_id
        )
        if found is None:
            msg = f"project not found: {project_id}"
            raise NotFound(msg)
        role, status = found
        _assert_project_writable(request, project_id, status)
        principal.cache_role(project_id, role)
    return project_id


ProjectScopeDep = Annotated[UUID, Depends(project_scope_dep)]


async def assert_stream_access(request: Request, project_id: UUID, principal: Principal) -> None:
    """Membership check for SSE streams that does NOT pin a pool connection.

    `ProjectScopeDep` pulls in the request-scoped `SessionDep`, which stays
    checked out until the request ends. An SSE request never ends, so every open
    stream would hold one of the (10+20) pool connections for its whole life —
    switching tabs/projects piles up streams and exhausts the pool, after which
    every query times out (QueuePool limit reached). Streams call this instead:
    it does the same membership check with a short-lived session that is released
    immediately, and the stream body acquires its own per-emit sessions.

    Streams bypass `ProjectScopeDep` entirely, so the credential-scope check has
    to be repeated here — otherwise the SSE endpoints become the hole.
    """
    # Its own stage, not `api.project_scope`: this is the path that exhausted
    # the connection pool by holding a session for the life of an SSE stream,
    # and a shared label would average that incident away against 111 ordinary
    # requests.
    with start_span(STAGE_STREAM_ACCESS):
        _assert_credential_scope(principal, project_id)
        maker = request.app.state.session_maker
        async with maker() as session:
            found = await get_member_role_and_status(
                session, project_id=project_id, user_id=principal.user_id
            )
        if found is None:
            msg = f"project not found: {project_id}"
            raise NotFound(msg)
        role, status = found
        # Streams are reads, so the write rule never fires here in practice —
        # but it is applied rather than skipped, because "streams bypass
        # ProjectScopeDep entirely" is exactly how the credential-scope hole
        # got in.
        _assert_project_writable(request, project_id, status)
        principal.cache_role(project_id, role)
