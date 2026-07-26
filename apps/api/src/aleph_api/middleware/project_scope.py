"""Project-scope helper: resolves and caches a Principal's role for a project_id.

This is a *dependency* (not an ASGI middleware) because it needs the FastAPI
path param. Use `project_scope_dep(project_id)` as a route dependency.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path, Request

from aleph_api.deps import PrincipalDep, SessionDep
from aleph_core.errors import NotFound
from aleph_db.repos.project import get_member
from aleph_security.principal import Principal


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
) -> UUID:
    """Resolve membership; cache role on principal.

    Returns 404 (NotFound) if the principal is not a member of the
    project — never leak existence across project scopes.
    """
    _assert_credential_scope(principal, project_id)
    member = await get_member(session, project_id=project_id, user_id=principal.user_id)
    if member is None:
        msg = f"project not found: {project_id}"
        raise NotFound(msg)
    principal.cache_role(project_id, member.role)
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
    _assert_credential_scope(principal, project_id)
    maker = request.app.state.session_maker
    async with maker() as session:
        member = await get_member(session, project_id=project_id, user_id=principal.user_id)
    if member is None:
        msg = f"project not found: {project_id}"
        raise NotFound(msg)
    principal.cache_role(project_id, member.role)
