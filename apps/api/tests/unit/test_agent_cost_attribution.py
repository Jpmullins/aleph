"""No model call made during a chat turn may be written unattributed.

Found by measurement, not by reading. `scripts/_acceptance/agent_turn_probe.py`
drove three real turns and the ledger came back with 13 attributed
`assistant.turn` rows sitting next to 9 orphans — priced correctly, belonging to
nothing:

    assistant.corpus_search.query_embed | gateway | attributed=f | 3
    assistant.page_selection            | gateway | attributed=f | 3
    assistant.compose                   | gateway | attributed=f | 3

All nine came from a single tool. `search_knowledge` calls
`WikiFirstRetrievalRouter.retrieve(..., agent_run_id=None)`, and the router
makes three further model calls of its own. The agent middleware never sees
them: it wraps the ORCHESTRATOR's model call, and these happen one layer down,
inside a tool.

`None` here is not a null, it is a claim — that this call belongs to no run —
and it is false for every call made during a turn. It is also invisible: the row
is written, the cost is right, the total is right, and only a per-run breakdown
ever shows the gap. That is the same shape as the original defect, where
`model_calls.agent_run_id` was NULL for the whole life of the column.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from typing import ClassVar

import pytest

from aleph_api import copilot_agent
from aleph_core.errors import PermissionDenied
from aleph_security.principal import Principal
from aleph_security.request_context import bind_principal, reset_principal

AGENT = pathlib.Path(__file__).resolve().parents[3] / "api/src/aleph_api/copilot_agent.py"


def test_no_tool_passes_a_null_run_id() -> None:
    """An AST check, because a grep cannot tell a call from a docstring.

    Scoped to `copilot_agent.py` and only to it. Elsewhere `agent_run_id=None`
    is correct and stays: `routes/assistant.py` serves the non-agent chat path
    and `routes/smoketest.py` is a diagnostic — neither happens during a turn,
    so neither has a run to name. A sweep over the whole tree would be red for
    two honest reasons and get suppressed.
    """
    tree = ast.parse(AGENT.read_text(), filename=str(AGENT))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "agent_run_id":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is None:
                func = ast.unparse(node.func)
                offenders.append(f"{func} at line {node.lineno}")
    assert not offenders, (
        "these calls claim to belong to no agent run, during a turn that has one: "
        + ", ".join(offenders)
    )


def test_the_search_tool_threads_the_run_id() -> None:
    """The positive half: it passes the run id, from the documented reader.

    Asserted separately from the sweep above because deleting the argument
    entirely would satisfy the sweep — `retrieve` has a default — while
    reintroducing the identical defect.
    """
    tree = ast.parse(AGENT.read_text(), filename=str(AGENT))
    threaded = [
        ast.unparse(kw.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "agent_run_id"
    ]
    assert "run_id_from_config(config)" in threaded, threaded


# ---------------------------------------------------------------------------
# WS-D2 c5 — the spend belongs to whoever asked, not to a user who is not there
# ---------------------------------------------------------------------------
#
# `_read_wiki_impl` used to build its own `Principal` around
# `uuid5(NAMESPACE_DNS, "dev@aleph.local")` and hand that to
# `WikiFirstRetrievalRouter.retrieve`, which threads it through every model call
# it makes. That id is not the local-dev user's: the auth middleware
# JIT-provisions the row and keeps the id the database assigned. Measured on
# this instance before the fix:
#
#     select count(*) from action_ledger_events
#      where actor_id = 'f48cb55b-af47-5e98-9f43-778743c4f744';   -> 32
#     select count(*) from users
#      where id       = 'f48cb55b-af47-5e98-9f43-778743c4f744';   ->  0
#
# The two tests below drive the real impls — no fixture is asserted — and pin
# that the principal reaching the service call is the one bound to the task.
#
# What that actually changes, stated precisely, because the plan overstates it:
# `model_calls` has no actor column and `LiteLLMClient.chat/.embed` open with
# `del principal`, so the router's principal reaches no cost row today. The rows
# that change now are the hypothesis writers' `ActionLedgerEvent.actor_id`. And
# the 32 rows counted above are `assistant.turn`, written by
# `ChatRunRecorder(actor_id=_dev_actor_id())` in `copilotkit_endpoint.py` — the
# same constant, a fourth call site, and one this change does not close.


class _FakeMember:
    def __init__(self, role: str) -> None:
        self.role = role


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeSession:
    def __init__(self, profile: object = None) -> None:
        self._profile = profile
        self.committed = False

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._profile)

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionMaker:
    def __init__(self, profile: object = None) -> None:
        self._session = _FakeSession(profile)

    def __call__(self) -> _FakeSessionMaker:
        return self

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeProfile:
    project_id = None
    bindings_jsonb: ClassVar[dict[str, object]] = {}


@pytest.fixture
def caller():
    """A bound principal with a user id that is nobody's synthetic constant."""
    p = Principal(
        user_id=uuid.uuid4(),
        subject="analyst@aleph.local",
        email="analyst@aleph.local",
        actor_kind="user",
    )
    token = bind_principal(p)
    yield p
    reset_principal(token)


def _bind_member(monkeypatch, session_maker) -> None:
    import aleph_db.repos.project as project_repo

    async def fake_get_member(_session: object, *, project_id: object, user_id: object):
        return _FakeMember("owner")

    monkeypatch.setattr(project_repo, "get_member", fake_get_member)
    monkeypatch.setitem(copilot_agent._runtime, "session_maker", session_maker)


class _Result:
    coverage_judgment = "ok"
    composed_body_md = "body"


def _patch_router(monkeypatch, seen: dict[str, object]) -> None:
    """Stand in for `WikiFirstRetrievalRouter`, recording what it was handed.

    Patched in the *unauthenticated* test too, so that a fix which fails open
    reports "DID NOT RAISE" rather than tripping over the fake session three
    layers into the real router — a red for the wrong reason reads as a flake
    and gets deleted.
    """
    import aleph_assistant.retrieval.router as router_mod

    class _FakeRouter:
        def __init__(self, **_kw: object) -> None: ...

        async def retrieve(self, **kwargs: object) -> _Result:
            seen.update(kwargs)
            return _Result()

    monkeypatch.setattr(router_mod, "WikiFirstRetrievalRouter", _FakeRouter)


@pytest.mark.asyncio
async def test_retrieval_attributed_to_caller(caller, monkeypatch) -> None:
    """The router runs as the authenticated caller, not as a fabricated user."""
    project_id = uuid.uuid4()
    seen: dict[str, object] = {}

    _patch_router(monkeypatch, seen)
    _bind_member(monkeypatch, _FakeSessionMaker(_FakeProfile()))
    monkeypatch.setitem(copilot_agent._runtime, "litellm", object())

    out = await copilot_agent._read_wiki_impl(
        "what do we know?",
        {"configurable": {"project_id": str(project_id)}},
    )

    assert "body" in out, out
    assert seen["principal"] is caller
    # The exact wrong value, named so the pin cannot drift back onto it.
    assert seen["principal"].user_id != copilot_agent._DEV_USER_UUID




@pytest.mark.asyncio
async def test_an_unauthenticated_caller_gets_no_substitute(monkeypatch) -> None:
    """No principal in context is a refusal, never a fabricated stand-in.

    Guards the shape of the fix as much as the fix: replacing `_dev_principal`
    with a *fallback* to it would satisfy both tests above and leave the hole
    open for exactly the case that produced the phantom rows — a tool running
    with nobody's authority.

    Two independent defences hold this, and it takes both failing open to turn
    this test red: `_project_id_from_config` -> `_authorized` refuses an
    unbound principal before the impl reaches `_acting_principal`, and
    `_acting_principal` refuses again. That is deliberate depth, not
    redundancy — the impls are also reachable from the subagents — but it means
    a mutation of either one alone leaves this green. Mutating both reports
    `DID NOT RAISE`.
    """
    _patch_router(monkeypatch, {})
    _bind_member(monkeypatch, _FakeSessionMaker(_FakeProfile()))
    monkeypatch.setitem(copilot_agent._runtime, "litellm", object())

    with pytest.raises(PermissionDenied):
        await copilot_agent._read_wiki_impl(
            "what do we know?",
            {"configurable": {"project_id": str(uuid.uuid4())}},
        )
