"""Principal — the authenticated subject of a request or job."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

ActorKind = Literal["user", "aleph_agent", "aiq_agent", "system"]


@dataclass
class Principal:
    """Resolved authenticated identity.

    Construct via the auth middleware (`verify_user_jwt`) for users
    or `verify_agent_token` for worker/AIQ agents. Role lookups
    happen in the project-scope middleware and are cached on this
    instance for the request's lifetime.
    """

    user_id: UUID
    subject: str
    email: str
    actor_kind: ActorKind
    # When actor_kind != "user", these identify the operation context.
    agent_run_id: UUID | None = None
    correlation_id: str | None = None

    _role_cache: dict[UUID, str] = field(default_factory=dict, compare=False, repr=False)

    def role_in(self, project_id: UUID) -> str | None:
        return self._role_cache.get(project_id)

    def cache_role(self, project_id: UUID, role: str | None) -> None:
        if role is None:
            self._role_cache.pop(project_id, None)
        else:
            self._role_cache[project_id] = role
