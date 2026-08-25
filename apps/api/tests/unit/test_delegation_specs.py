"""The credential must be the caller's, and the graph is shared. Hence this file.

`agent_resolution_signature` keys the compiled-graph cache on
`(endpoint, bindings, api_key)` — NOT on the caller. One graph therefore answers
for every user of a project. A token baked into an `AsyncSubAgent` spec at
graph-build time would attribute every delegation any of them started to whoever
happened to build the graph, and it would be invisible: the run would succeed and
the ledger would simply name the wrong person.

That is the same defect class as taking agent scope from a client-supplied thread
id, which this repository has already shipped once. So the tests that matter here
are about isolation and laziness, not about the happy path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from aleph_api.delegation import (
    _TOKENS,
    async_subagent_specs,
    current_turn_scope,
    set_turn_scope,
)
from aleph_security.agent_token import verify_agent_token

SECRET = "unit-test-delegation-secret-not-used-anywhere-else"


def _settings(**over: object) -> SimpleNamespace:
    base = {
        "aleph_api_internal_url": "http://api.internal:8000",
        "aleph_agent_token_secret": SECRET,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clean() -> None:
    _TOKENS.clear()


def test_specs_are_shaped_the_way_deepagents_demultiplexes_them() -> None:
    """`create_deep_agent` routes a spec to the async path on `"graph_id" in spec`.

    These are Mappings rather than dicts, so `in` has to work — a plain object
    with attributes would be silently treated as a SYNC subagent and mounted
    into `task`, where it would block the supervisor forever.
    """
    specs = async_subagent_specs(_settings())
    assert specs, "at least one delegatable subagent must be offered"
    for spec in specs:
        assert "graph_id" in spec
        assert spec["url"] == "http://api.internal:8000/v1/agent-protocol"
        assert spec["name"] == spec["graph_id"]
        assert len(spec["description"]) > 40, "the supervisor picks by reading this"


def test_headers_are_empty_with_no_turn_scope() -> None:
    """No turn means nothing legitimate is delegating.

    Empty headers make the route answer 403. The alternative — minting a token
    from some ambient default — is how an unscoped call gets through.
    """
    spec = async_subagent_specs(_settings())[0]
    assert current_turn_scope() is None
    assert spec["headers"] == {}


def test_the_token_carries_the_caller_and_the_project() -> None:
    project, user = uuid4(), uuid4()
    set_turn_scope(project_id=project, user_id=user)
    spec = async_subagent_specs(_settings())[0]
    token = spec["headers"]["Authorization"].removeprefix("Bearer ")
    claims = verify_agent_token(token, secret=SECRET)
    assert claims.project_id == project
    assert claims.user_id == user


def test_two_callers_in_one_process_do_not_share_a_credential() -> None:
    """The whole point of the ContextVar.

    Turns run concurrently in one process. A module global here would hand one
    caller's token to another, and both requests would succeed — so nothing
    would ever report it.
    """
    spec = async_subagent_specs(_settings())[0]
    a_project, a_user = uuid4(), uuid4()
    b_project, b_user = uuid4(), uuid4()
    seen: dict[str, UUID] = {}

    async def turn(tag: str, project: UUID, user: UUID) -> None:
        set_turn_scope(project_id=project, user_id=user)
        await asyncio.sleep(0)  # yield, so the two turns interleave
        token = spec["headers"]["Authorization"].removeprefix("Bearer ")
        seen[tag] = verify_agent_token(token, secret=SECRET).project_id

    async def both() -> None:
        await asyncio.gather(
            asyncio.create_task(turn("a", a_project, a_user)),
            asyncio.create_task(turn("b", b_project, b_user)),
        )

    asyncio.run(both())
    assert seen["a"] == a_project
    assert seen["b"] == b_project


def test_the_token_is_reused_within_its_bucket() -> None:
    """`_ClientCache` keys httpx clients on (url, headers) and NEVER evicts.

    A freshly minted token per turn means a new client per turn, retained for the
    life of a process that runs for weeks. Reuse within a window is what bounds
    that, and it is the reason this is not just `mint_agent_token(...)` inline.
    """
    project, user = uuid4(), uuid4()
    set_turn_scope(project_id=project, user_id=user)
    spec = async_subagent_specs(_settings())[0]
    first = spec["headers"]["Authorization"]
    second = spec["headers"]["Authorization"]
    assert first == second, "the header must be stable within a bucket"
    assert len(_TOKENS) == 1


def test_stale_buckets_are_evicted() -> None:
    """Otherwise the map grows by one entry per (project, user) per half hour."""
    project, user = uuid4(), uuid4()
    set_turn_scope(project_id=project, user_id=user)
    spec = async_subagent_specs(_settings())[0]
    _TOKENS[(project, user, 1)] = "an-old-bucket"
    _TOKENS[(project, user, 2)] = "an-older-one"
    spec["headers"]
    assert len(_TOKENS) == 1, f"stale buckets survived: {list(_TOKENS)}"


def test_no_loopback_configuration_means_no_delegation_tools() -> None:
    """A deployment that cannot delegate must not offer the tools.

    `AsyncSubAgentMiddleware` is mounted only when an async spec exists, so
    returning nothing leaves a supervisor that never advertises a capability it
    cannot deliver — rather than one that offers five tools and fails on the
    first call.
    """
    assert async_subagent_specs(_settings(aleph_api_internal_url="")) == []
    assert async_subagent_specs(_settings(aleph_agent_token_secret="")) == []
    assert async_subagent_specs(SimpleNamespace()) == []


def test_every_delegatable_subagent_is_offered() -> None:
    """The registry and the specs cannot disagree about what exists.

    A subagent the route accepts but that is never offered is unreachable; one
    offered but not accepted produces a run that can never resolve.
    """
    from aleph_api.subagents import DELEGATABLE_SUBAGENTS

    offered = {spec["graph_id"] for spec in async_subagent_specs(_settings())}
    assert offered == set(DELEGATABLE_SUBAGENTS)
