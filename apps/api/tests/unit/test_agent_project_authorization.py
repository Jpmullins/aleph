"""The agent path must authorize a real member, not just reject a forged id.

F1 closed a genuine bypass: `/copilotkit` took its project id from client-supplied
config. But its test only asserted that a *forged* id is denied — a property
"deny everyone" also satisfies. And deny-everyone is exactly what shipped: the
role comes from `Principal.role_in`, which is filled by `project_scope_dep`, a
FastAPI dependency keyed on a `project_id` **path param** that the agent endpoint
does not have. Nothing called `cache_role`, so every agent tool raised
PermissionDenied for legitimate users and the chat was unusable.

So this pins both directions.
"""

from __future__ import annotations

import uuid

import pytest

from aleph_api import copilot_agent
from aleph_core.errors import PermissionDenied
from aleph_security.principal import Principal
from aleph_security.request_context import bind_principal, reset_principal


class _FakeMember:
    def __init__(self, role: str) -> None:
        self.role = role


class _FakeSessionMaker:
    """Stands in for the runtime's async_sessionmaker."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def principal():
    p = Principal(
        user_id=uuid.uuid4(),
        subject="analyst@aleph.local",
        email="analyst@aleph.local",
        actor_kind="user",
    )
    token = bind_principal(p)
    yield p
    reset_principal(token)


def _patch(monkeypatch, member):
    import aleph_db.repos.project as project_repo

    async def fake_get_member(_session, *, project_id, user_id):
        return member

    monkeypatch.setattr(project_repo, "get_member", fake_get_member)
    monkeypatch.setitem(copilot_agent._runtime, "session_maker", _FakeSessionMaker())


@pytest.mark.asyncio
async def test_member_is_authorized_and_role_is_cached(principal, monkeypatch) -> None:
    """The regression: a real member must be allowed through."""
    project_id = uuid.uuid4()
    _patch(monkeypatch, _FakeMember("owner"))

    assert principal.role_in(project_id) is None  # nothing has cached it yet
    assert await copilot_agent._authorized(project_id) == project_id
    # Cached, so the next tool call in the same run does not re-query.
    assert principal.role_in(project_id) == "owner"


@pytest.mark.asyncio
async def test_non_member_is_denied(principal, monkeypatch) -> None:
    project_id = uuid.uuid4()
    _patch(monkeypatch, None)

    with pytest.raises(PermissionDenied):
        await copilot_agent._authorized(project_id)


@pytest.mark.asyncio
async def test_unbound_principal_is_denied(monkeypatch) -> None:
    """No principal in context means nobody's authority — never 'allow'."""
    _patch(monkeypatch, _FakeMember("owner"))
    with pytest.raises(PermissionDenied):
        await copilot_agent._authorized(uuid.uuid4())
