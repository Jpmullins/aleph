"""The assistant's conversation state must be durable, and must stay durable.

`build_assistant_deep_agent` accepts an optional `checkpointer` and falls back to
an in-memory `MemorySaver` when none is passed — a convenience so tests can
compile the graph without a database. That fallback is also a silent-reversion
hazard of exactly the kind this codebase keeps getting bitten by: drop the
argument anywhere on the production path and nothing fails, nothing logs, and
every API restart quietly discards the agent's conversation history, its
`write_todos` plan (what the Activity card renders), and the summarization
archive.

These tests pin the *production wiring*, not the function's signature: it is the
wiring that regresses.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
LIFESPAN = REPO_ROOT / "apps" / "api" / "src" / "aleph_api" / "lifespan.py"
ENDPOINT = REPO_ROOT / "apps" / "api" / "src" / "aleph_api" / "copilotkit_endpoint.py"


def _calls_named(tree: ast.Module, name: str) -> list[ast.Call]:
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname == name:
                out.append(node)
    return out


def _kwarg_names(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg}


def test_paths_exist() -> None:
    """Guard the guard — a moved file would make every assertion vacuous."""
    assert LIFESPAN.is_file(), LIFESPAN
    assert ENDPOINT.is_file(), ENDPOINT


def test_lifespan_builds_a_durable_checkpointer() -> None:
    tree = ast.parse(LIFESPAN.read_text(encoding="utf-8"))
    assert _calls_named(tree, "build_agent_checkpointer"), (
        "lifespan does not build a checkpointer — the agent will fall back to "
        "in-memory state and lose every conversation on restart."
    )


def test_lifespan_runs_checkpointer_setup() -> None:
    """`AsyncPostgresSaver` needs its tables created before first use."""
    src = LIFESPAN.read_text(encoding="utf-8")
    assert "agent_checkpointer.setup()" in src, (
        "lifespan never calls `setup()` on the checkpointer; its tables would "
        "not exist and the first agent turn would fail at runtime."
    )


def test_lifespan_passes_the_checkpointer_to_the_endpoint() -> None:
    tree = ast.parse(LIFESPAN.read_text(encoding="utf-8"))
    calls = _calls_named(tree, "setup_copilotkit")
    assert calls, "lifespan does not mount the agent endpoint at all"
    for call in calls:
        assert "checkpointer" in _kwarg_names(call), (
            "setup_copilotkit is called without `checkpointer=` — the agent "
            "silently reverts to MemorySaver."
        )


def test_endpoint_forwards_the_checkpointer_to_the_graph() -> None:
    tree = ast.parse(ENDPOINT.read_text(encoding="utf-8"))
    calls = _calls_named(tree, "build_assistant_deep_agent")
    assert calls, "the endpoint does not build the agent graph"
    for call in calls:
        assert "checkpointer" in _kwarg_names(call), (
            "build_assistant_deep_agent is called without `checkpointer=` — the "
            "durable saver stops at the endpoint and never reaches the graph."
        )


def test_checkpointer_is_postgres_backed() -> None:
    """The durable saver must be the Postgres one, not another in-memory shim."""
    from aleph_api.copilot_agent import build_agent_checkpointer

    src = inspect.getsource(build_agent_checkpointer)
    assert "AsyncPostgresSaver" in src, (
        f"build_agent_checkpointer does not return an AsyncPostgresSaver:\n{src}"
    )


def test_checkpointer_reuses_the_store_pool() -> None:
    """Opening a second pool for the saver would double the connection budget.

    `build_agent_store` already configures exactly what `AsyncPostgresSaver`
    wants (autocommit, no prepared statements, dict rows), so the saver takes
    the pool as an argument rather than a connection string.
    """
    from aleph_api.copilot_agent import build_agent_checkpointer

    params = list(inspect.signature(build_agent_checkpointer).parameters)
    assert params == ["pool"], (
        f"expected build_agent_checkpointer(pool), got {params} — a saver that "
        "opens its own pool doubles the API's Postgres connection usage."
    )


def test_memorysaver_is_only_a_fallback_never_the_production_path() -> None:
    """`MemorySaver` may appear only as the no-checkpointer default.

    If it ever becomes unconditional again, durability is gone with no other
    signal — which is precisely how it shipped before.
    """
    src = (REPO_ROOT / "apps" / "api" / "src" / "aleph_api" / "copilot_agent.py").read_text(
        encoding="utf-8"
    )
    assert "checkpointer=MemorySaver()," not in src, (
        "copilot_agent passes MemorySaver unconditionally — the durable "
        "checkpointer is bypassed and conversations are lost on restart."
    )
