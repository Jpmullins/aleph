"""Principal — the authenticated subject of a request or job."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

ActorKind = Literal["user", "aleph_agent", "system"]


@dataclass
class Principal:
    """Resolved authenticated identity.

    Construct via the auth middleware (`verify_user_jwt`) for users
    or `verify_agent_token` for worker agents. Role lookups
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
    #: The project this *credential* is bound to, when it is bound to one.
    #:
    #: Agent tokens are minted per project (OWNER-gated) and signed with a
    #: `project_id` claim. Carrying it here is what makes that binding mean
    #: something at use time: `project_scope_dep` / `assert_stream_access`
    #: refuse any other project outright, before membership is even queried.
    #: Without it a worker's hour-long token authorized every project its
    #: underlying user belonged to.
    #:
    #: `None` for human principals — they are unscoped by credential and
    #: governed purely by project membership.
    project_id: UUID | None = None

    _role_cache: dict[UUID, str] = field(default_factory=dict, compare=False, repr=False)

    def role_in(self, project_id: UUID) -> str | None:
        return self._role_cache.get(project_id)

    def cache_role(self, project_id: UUID, role: str | None) -> None:
        if role is None:
            self._role_cache.pop(project_id, None)
        else:
            self._role_cache[project_id] = role
