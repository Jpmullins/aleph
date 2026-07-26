"""An agent token is bound to one project, and that binding is enforced at use.

`mint_agent_token` signs a `project_id` and `POST /v1/agent-tokens` gates minting
on OWNER of that project. But `verify_agent_token` returned the claim and the
auth middleware **discarded it**: `Principal` had no project field and nothing
compared the token's project to the route's. A token minted for project A —
workers hold these for up to an hour — therefore authorized every route of every
*other* project its underlying user belonged to, and the mint-time OWNER gate
was decorative.

The scope check runs *before* the membership query, so these tests use a session
stub that raises if touched: reaching the DB at all would mean the cheap refusal
did not happen.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from aleph_core.errors import NotFound
from aleph_security.principal import Principal


class _ExplodingSession:
    """Any DB access here means the scope check failed to short-circuit."""

    async def execute(self, *_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("membership was queried before the token's project scope was checked")


def _agent_principal(scoped_to):
    return Principal(
        user_id=uuid4(),
        subject="agent",
        email="agent@aleph.local",
        actor_kind="aleph_agent",
        agent_run_id=uuid4(),
        project_id=scoped_to,
    )


def test_principal_carries_a_project_scope() -> None:
    """The field must exist, default to None, and be settable."""
    pid = uuid4()
    assert _agent_principal(pid).project_id == pid
    unscoped = Principal(user_id=uuid4(), subject="u", email="u@x", actor_kind="user")
    assert unscoped.project_id is None, "user principals must stay unscoped"


def _fake_request(method: str = "POST", path: str = "/v1/projects/x/sources"):
    """Minimal request for `project_scope_dep`.

    A write by default: these tests assert the credential-scope refusal fires
    before anything else, and a write is the case where the newer project-status
    check could otherwise mask it.
    """
    from types import SimpleNamespace

    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


async def test_agent_token_refused_on_another_project() -> None:
    from aleph_api.middleware.project_scope import project_scope_dep

    project_a, project_b = uuid4(), uuid4()
    principal = _agent_principal(project_a)

    with pytest.raises(NotFound):
        await project_scope_dep(project_b, principal, _ExplodingSession(), _fake_request())


async def test_agent_token_scope_checked_for_streams_too() -> None:
    """`assert_stream_access` is the SSE path's membership check and bypasses
    `ProjectScopeDep` entirely — it needs the same guard or streams become the
    hole."""
    from types import SimpleNamespace

    from aleph_api.middleware.project_scope import assert_stream_access

    project_a, project_b = uuid4(), uuid4()
    principal = _agent_principal(project_a)

    def _maker():  # pragma: no cover - must not run
        raise AssertionError("stream membership queried before the scope check")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=_maker)))

    with pytest.raises(NotFound):
        await assert_stream_access(request, project_b, principal)


async def test_unscoped_principal_still_reaches_membership() -> None:
    """A human principal has no token scope; membership must still govern.

    Proven by the session being reached — the opposite of the tests above.
    """
    from aleph_api.middleware.project_scope import project_scope_dep

    reached: list[bool] = []

    class _Session:
        async def execute(self, *_a, **_k):
            reached.append(True)

            class _R:
                # `project_scope_dep` resolves membership and project status in
                # ONE joined query now (it used to issue two), so the stub has to
                # answer `one_or_none` rather than `scalar_one_or_none`.
                @staticmethod
                def one_or_none():
                    return None

                @staticmethod
                def scalar_one_or_none():
                    return None

            return _R()

    principal = Principal(user_id=uuid4(), subject="u", email="u@x", actor_kind="user")
    with pytest.raises(NotFound):
        await project_scope_dep(uuid4(), principal, _Session(), _fake_request())
    assert reached, "an unscoped principal must fall through to the membership check"
