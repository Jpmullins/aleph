"""The authenticated principal, carried across a framework boundary.

Routes receive their principal by dependency injection. The AG-UI agent endpoint
cannot: it builds its own `RunnableConfig` and hands that to tools — so there is
no parameter to thread a principal through.

The consequence was a complete authorization bypass. `/copilotkit` sat on the
auth middleware's self-auth prefix list on the promise that "the route handler is
responsible for its own verification", the route handler performed none, and the
agent's tools took their project id from client-supplied `RunnableConfig`
metadata. Any caller could name any project.

A ContextVar is the right shape here: it is set once per request by the
middleware, is naturally task-scoped so concurrent requests cannot see each
other's principal, and is readable from a tool that has no request in scope.
The same pattern is already used for workflow context in `aleph-research` and
`aleph-wiki`.

**Reading this is not optional for an agent tool.** `require_project_access`
raises when no principal is bound, so a tool that forgets the check fails
closed rather than operating unscoped.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

from aleph_core.errors import PermissionDenied
from aleph_security.roles import ProjectRole, require_at_least

if TYPE_CHECKING:
    from uuid import UUID

    from aleph_security.principal import Principal

__all__ = [
    "bind_principal",
    "current_principal",
    "require_project_access",
    "reset_principal",
]

_principal_var: ContextVar[Principal | None] = ContextVar("aleph_principal", default=None)


def bind_principal(principal: Principal) -> Token[Principal | None]:
    """Bind the authenticated principal for this task. Returns a reset token."""
    return _principal_var.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    _principal_var.reset(token)


def current_principal() -> Principal | None:
    """The principal for this task, or None outside an authenticated request."""
    return _principal_var.get()


def require_project_access(
    project_id: UUID,
    *,
    at_least: ProjectRole = ProjectRole.VIEWER,
) -> Principal:
    """Authorize the current principal against ``project_id``, or raise.

    Fails closed on an unbound principal. An agent tool running outside a request
    has nobody's authority, and treating that as "allow" is how a client-supplied
    project id became a data-access primitive.
    """
    principal = current_principal()
    if principal is None:
        msg = (
            "no authenticated principal in context; refusing to act on "
            f"project {project_id} without one"
        )
        raise PermissionDenied(msg)
    require_at_least(principal, project_id, at_least=at_least)
    return principal
