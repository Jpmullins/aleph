"""The agent's Postgres pool held exactly one connection.

`AsyncConnectionPool` defaults `max_size` to `min_size`, and `build_agent_store`
passed only `min_size=1`. So every saved checkpoint, every memory read and every
one of six concurrent subagents queued behind the same single connection and
gave up after 30 seconds — which is the shape of "the assistant is slow and then
fails" that produces no error message anywhere.

These are construction tests, deliberately: the property is a number on a pool
object, and asserting it needs no database. A test that needed Postgres to check
a configured integer would be slower and prove the same thing.
"""

from __future__ import annotations

from aleph_api.copilot_agent import build_agent_store

URL = "postgresql+asyncpg://aleph:secret@localhost:5432/aleph"


class _Settings:
    aleph_agent_pool_min_size = 2
    aleph_agent_pool_max_size = 12
    aleph_agent_pool_timeout_s = 45.0


async def test_pool_max_size_is_not_one() -> None:
    pool, _store = build_agent_store(database_url=URL, settings=_Settings())
    assert pool.max_size >= 8, f"the agent pool holds {pool.max_size} connection(s)"
    assert pool.max_size > pool.min_size, (
        "max_size equal to min_size is the defect: the pool cannot grow under a subagent fan-out"
    )


async def test_the_pool_is_sized_from_settings_not_a_literal() -> None:
    """An operator has to be able to size this against their own Postgres."""
    pool, _store = build_agent_store(database_url=URL, settings=_Settings())
    assert pool.min_size == _Settings.aleph_agent_pool_min_size
    assert pool.max_size == _Settings.aleph_agent_pool_max_size


def test_the_default_settings_are_already_safe() -> None:
    """A deployment that configures nothing must not get the single-connection
    pool back by omission."""
    from aleph_api.settings import Settings

    defaults = Settings.model_fields
    assert defaults["aleph_agent_pool_max_size"].default >= 8
    assert (
        defaults["aleph_agent_pool_max_size"].default
        > defaults["aleph_agent_pool_min_size"].default
    )


async def test_the_langgraph_mandated_connection_kwargs_survive() -> None:
    """langgraph's own store requires autocommit, no prepared statements and
    dict rows. Adding sizing must not disturb them."""
    pool, _store = build_agent_store(database_url=URL, settings=_Settings())
    assert pool.kwargs["autocommit"] is True
    assert pool.kwargs["prepare_threshold"] == 0
    assert pool.kwargs["row_factory"] is not None


def test_no_model_timeout_or_retry_literal_remains() -> None:
    """Both were literals on the agent's ChatOpenAI. 60s is below the p99 of a
    tool-heavy turn, and two immediate retries is the worst possible response to
    being rate limited.

    Asserted over the ChatOpenAI CALL rather than over the whole file: an
    unrelated `httpx.AsyncClient(timeout=60.0)` elsewhere in the module would
    make a file-wide grep fail for a reason that has nothing to do with the
    agent's model budget, and a check that fails for the wrong reason gets
    weakened rather than read.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("apps/api/src/aleph_api/copilot_agent.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ChatOpenAI"
    ]
    assert calls, "no ChatOpenAI construction found — update this test"
    for call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        timeout = kwargs.get("timeout")
        assert not isinstance(timeout, ast.Constant), (
            f"timeout is the literal {getattr(timeout, 'value', None)!r}, not configuration"
        )
        retries = kwargs.get("max_retries")
        assert isinstance(retries, ast.Constant) and retries.value == 0, (
            "the SDK's own retry must be OFF: AlephAgentMiddleware owns the retry "
            "budget, and two budgets stacked multiply the request rate exactly "
            "when the gateway can least afford it"
        )
