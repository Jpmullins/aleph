"""ProjectRole enum and role-gate helpers."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from aleph_core.errors import PermissionDenied
from aleph_security.principal import Principal


class ProjectRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


_RANK: dict[str, int] = {
    ProjectRole.VIEWER.value: 1,
    ProjectRole.EDITOR.value: 2,
    ProjectRole.OWNER.value: 3,
}


def rank(role: str) -> int:
    return _RANK.get(role, 0)


def require_at_least(
    principal: Principal, project_id: UUID, *, at_least: ProjectRole
) -> None:
    """Raise PermissionDenied unless the principal's role in `project_id`
    is at least `at_least`. The middleware caches the role on the
    Principal; if absent the principal is not a member."""
    role = principal.role_in(project_id)
    if role is None or rank(role) < rank(at_least.value):
        msg = f"insufficient role for project {project_id}"
        raise PermissionDenied(msg)
