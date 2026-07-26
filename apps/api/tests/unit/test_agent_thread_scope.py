"""The project named in an AG-UI request must be one the caller belongs to.

Agent tools derive their project scope entirely from the *client-supplied*
thread id (`proj:<project_id>:<thread>`, parsed by
`copilot_agent._project_id_from_thread_id`) and then mint a self-call agent
token for it. Nothing compared that project against the authenticated caller,
so any authenticated user could drive the Deep Agent's write tools against any
project UUID simply by naming it in the thread id.

Closing the unauthenticated hole (`test_copilotkit_auth.py`) was necessary but
not sufficient: it stops anonymous callers, not authenticated ones reaching
across projects. This module covers the second half.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from aleph_api.middleware.agent_scope import (
    extract_project_ids,
    thread_project_id,
)


class TestThreadIdParsing:
    def test_extracts_project_from_prefixed_thread(self) -> None:
        pid = uuid4()
        assert thread_project_id(f"proj:{pid}:main") == pid

    @pytest.mark.parametrize(
        "value",
        ["", "main", "proj:", "proj:not-a-uuid:x", None, 123, f"PROJ:{uuid4()}:x"],
    )
    def test_rejects_everything_else(self, value: object) -> None:
        assert thread_project_id(value) is None


class TestBodyExtraction:
    """Every channel a caller can name a project through must be found.

    Missing one is the whole bug: an unchecked channel is an open door, so the
    extractor must be exhaustive rather than best-effort.
    """

    def test_finds_threadid_camel_and_snake(self) -> None:
        a, b = uuid4(), uuid4()
        body = json.dumps({"threadId": f"proj:{a}:x", "thread_id": f"proj:{b}:y"}).encode()
        assert extract_project_ids(body) == {a, b}

    def test_finds_explicit_project_id_keys(self) -> None:
        a, b = uuid4(), uuid4()
        body = json.dumps({"projectId": str(a), "config": {"project_id": str(b)}}).encode()
        assert extract_project_ids(body) == {a, b}

    def test_finds_nested_and_listed_occurrences(self) -> None:
        a, b = uuid4(), uuid4()
        body = json.dumps(
            {"messages": [{"meta": {"threadId": f"proj:{a}:x"}}], "d": {"e": {"projectId": str(b)}}}
        ).encode()
        assert extract_project_ids(body) == {a, b}

    @pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"{}", b"null"])
    def test_no_ids_from_junk(self, body: bytes) -> None:
        assert extract_project_ids(body) == set()

    def test_unprefixed_thread_yields_nothing(self) -> None:
        """A plain thread id names no project and must not be inventable."""
        assert extract_project_ids(json.dumps({"threadId": "main"}).encode()) == set()


class TestEnforcement:
    async def test_refuses_project_the_caller_is_not_in(self) -> None:
        from aleph_api.middleware.agent_scope import assert_caller_may_use_projects
        from aleph_core.errors import NotFound
        from aleph_security.principal import Principal

        principal = Principal(user_id=uuid4(), subject="u", email="u@x", actor_kind="user")

        async def _is_member(_uid, _pid) -> bool:
            return False

        with pytest.raises(NotFound):
            await assert_caller_may_use_projects(principal, {uuid4()}, _is_member)

    async def test_allows_project_the_caller_is_in(self) -> None:
        from aleph_api.middleware.agent_scope import assert_caller_may_use_projects
        from aleph_security.principal import Principal

        pid = uuid4()
        principal = Principal(user_id=uuid4(), subject="u", email="u@x", actor_kind="user")

        async def _is_member(_uid, p) -> bool:
            return p == pid

        await assert_caller_may_use_projects(principal, {pid}, _is_member)

    async def test_all_named_projects_must_pass_not_just_one(self) -> None:
        """A request naming two projects must be a member of BOTH.

        Otherwise a caller smuggles an unauthorized project alongside an
        authorized one and the check passes on the wrong element.
        """
        from aleph_api.middleware.agent_scope import assert_caller_may_use_projects
        from aleph_core.errors import NotFound
        from aleph_security.principal import Principal

        allowed, forbidden = uuid4(), uuid4()
        principal = Principal(user_id=uuid4(), subject="u", email="u@x", actor_kind="user")

        async def _is_member(_uid, p) -> bool:
            return p == allowed

        with pytest.raises(NotFound):
            await assert_caller_may_use_projects(principal, {allowed, forbidden}, _is_member)

    async def test_empty_set_is_allowed(self) -> None:
        """Naming no project is fine — the tools then have no scope to act in."""
        from aleph_api.middleware.agent_scope import assert_caller_may_use_projects
        from aleph_security.principal import Principal

        principal = Principal(user_id=uuid4(), subject="u", email="u@x", actor_kind="user")

        async def _is_member(_uid, _p) -> bool:  # pragma: no cover - must not run
            raise AssertionError("membership queried for an empty project set")

        await assert_caller_may_use_projects(principal, set(), _is_member)


def test_thread_parsers_agree() -> None:
    """This module and the agent must parse thread ids identically.

    If they diverge, the agent acts on a project this check never inspected —
    which is the exact hole, reopened by drift instead of by omission.
    """
    from aleph_api.copilot_agent import _project_id_from_thread_id

    pid = uuid4()
    cases = [
        f"proj:{pid}:main",
        f"proj:{pid}:",
        "proj:not-a-uuid:x",
        "proj:",
        "main",
        "",
        None,
        12345,
        f"PROJ:{pid}:x",
        f"  proj:{pid}:x",
    ]
    for case in cases:
        assert thread_project_id(case) == _project_id_from_thread_id(case), (
            f"parsers disagree on {case!r}: agent_scope={thread_project_id(case)} "
            f"copilot_agent={_project_id_from_thread_id(case)}"
        )


class TestWiring:
    """The check must be REACHABLE, not merely defined.

    A correct guard that no request path invokes is this codebase's signature
    failure mode, so these drive the real app through the real middleware.
    """

    @staticmethod
    def _app(*, member: bool):
        from types import SimpleNamespace

        from aleph_api.main import create_app

        app = create_app()
        app.state.settings = SimpleNamespace(
            aleph_auth_mode="local",
            local_dev_subject="dev@aleph.local",
            local_dev_email="dev@aleph.local",
            local_dev_display_name="Dev",
        )

        # The middleware first resolves a local principal (a User lookup, which
        # must succeed so provisioning short-circuits) and then checks
        # membership (which is what this test varies).
        existing_user = SimpleNamespace(
            id=uuid4(),
            subject="dev@aleph.local",
            email="dev@aleph.local",
            display_name="Dev",
        )
        state = {"calls": 0}

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def execute(self, *_a, **_k):
                state["calls"] += 1
                first = state["calls"] == 1
                row = (
                    existing_user if first else (SimpleNamespace(role="owner") if member else None)
                )

                class _R:
                    @staticmethod
                    def scalar_one_or_none():
                        return row

                return _R()

        app.state.session_maker = lambda: _Session()
        return app

    async def test_foreign_project_in_thread_id_is_refused(self) -> None:
        import httpx

        app = self._app(member=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/copilotkit/agent/assistant",
                json={"threadId": f"proj:{uuid4()}:main", "messages": []},
            )
        # NOT just `== 404`: the AG-UI route is mounted at lifespan startup,
        # which these tests do not run, so FastAPI's router also 404s. The two
        # are only distinguishable by the body — asserting on the status alone
        # would pass for entirely the wrong reason.
        assert self._refused_by_scope_check(resp), (
            f"a request naming a foreign project was not refused by the scope "
            f"check (got {resp.status_code} {resp.text[:120]}); the guard is "
            "not reachable from the request path"
        )

    @staticmethod
    def _refused_by_scope_check(resp) -> bool:
        """True only for OUR refusal, not the router's missing-route 404."""
        return (
            resp.status_code == 404
            and resp.headers.get("content-type", "").startswith("application/problem+json")
            and resp.json().get("detail", "").startswith("project not found:")
        )

    async def test_member_project_is_not_refused(self) -> None:
        """The negative test above must not be passing vacuously.

        A guard that refused everything would satisfy it while breaking the
        product, so prove a member's own project gets past the scope check.
        """
        import httpx

        app = self._app(member=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/copilotkit/agent/assistant",
                json={"threadId": f"proj:{uuid4()}:main", "messages": []},
            )
        assert not self._refused_by_scope_check(resp), (
            "a member's own project was refused by the agent-scope check — the "
            "guard is over-broad and would break every chat turn"
        )

    async def test_request_naming_no_project_is_not_refused(self) -> None:
        """No project named ⇒ no scope to check ⇒ no membership query."""
        import httpx

        app = self._app(member=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post("/copilotkit/agent/assistant", json={"messages": []})
        assert not self._refused_by_scope_check(resp)
