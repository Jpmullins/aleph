"""The principal context — fails closed, and does not leak between tasks.

These pin the fix for a complete authorization bypass: `/copilotkit` was exempt
from the auth middleware on the stated promise that the route handler verified
(it did not), and the agent's tools took their project id from client-supplied
RunnableConfig metadata. Any caller could name any project.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from aleph_core.errors import PermissionDenied
from aleph_security.principal import Principal
from aleph_security.request_context import (
    bind_principal,
    current_principal,
    require_project_access,
    reset_principal,
)
from aleph_security.roles import ProjectRole


def principal_for(project_id, role: str = "editor") -> Principal:
    """A principal whose role cache is populated, as the scope middleware does."""
    principal = Principal(
        user_id=uuid4(),
        subject="test",
        email="t@aleph.local",
        actor_kind="user",
    )
    principal.cache_role(project_id, role)
    return principal


def test_no_principal_is_denied_not_allowed() -> None:
    """THE property. An agent tool with nobody's authority must not proceed."""
    assert current_principal() is None
    with pytest.raises(PermissionDenied, match="no authenticated principal"):
        require_project_access(uuid4())


def test_a_member_is_allowed() -> None:
    project = uuid4()
    token = bind_principal(principal_for(project))
    try:
        assert require_project_access(project) is not None
    finally:
        reset_principal(token)


def test_naming_someone_elses_project_is_denied() -> None:
    """The exact bypass: a client-supplied id for a project you are not in."""
    mine, theirs = uuid4(), uuid4()
    token = bind_principal(principal_for(mine))
    try:
        with pytest.raises(PermissionDenied, match="insufficient role"):
            require_project_access(theirs)
    finally:
        reset_principal(token)


def test_insufficient_role_is_denied() -> None:
    project = uuid4()
    token = bind_principal(principal_for(project, role="viewer"))
    try:
        with pytest.raises(PermissionDenied, match="insufficient role"):
            require_project_access(project, at_least=ProjectRole.EDITOR)
    finally:
        reset_principal(token)


def test_reset_restores_the_previous_principal() -> None:
    project = uuid4()
    token = bind_principal(principal_for(project))
    reset_principal(token)
    assert current_principal() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_do_not_see_each_others_principal() -> None:
    """A pooled worker task must not inherit the previous request's identity."""
    a_project, b_project = uuid4(), uuid4()
    seen: dict[str, object] = {}

    async def run(name: str, project) -> None:
        token = bind_principal(principal_for(project))
        try:
            await asyncio.sleep(0)  # force interleaving
            seen[name] = require_project_access(project).role_in(project)
        finally:
            reset_principal(token)

    await asyncio.gather(run("a", a_project), run("b", b_project))
    # Each task authorized against its own project. If the ContextVar leaked,
    # one of the two would have raised PermissionDenied instead.
    assert seen == {"a": "editor", "b": "editor"}


def test_the_copilotkit_exemption_is_gone() -> None:
    """The middleware must not exempt the agent endpoint from authentication.

    Kept as a test rather than a grep in CI so the reason travels with the
    assertion: an entry here needs a handler that demonstrably verifies.
    """
    from aleph_api.middleware.auth import _SELF_AUTH_PREFIXES

    assert _SELF_AUTH_PREFIXES == (), (
        f"auth middleware exempts {_SELF_AUTH_PREFIXES}; an exemption requires a "
        f"route handler that actually verifies, plus a test proving it"
    )
