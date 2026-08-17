"""Concurrent workflow runs must not share a context.

Every graph workflow reaches its dependencies (session maker, LiteLLM client,
principal) through a module-level `_ctx()` rather than threading them through
state. Four of the six stored that context in a **module global**:

    global _active_ctx
    _active_ctx = self._ctx
    try:    ...await the whole graph...
    finally: _active_ctx = None

arq runs jobs concurrently in one event loop. So:

    job A sets the global, awaits
    job B sets the global, awaits
    job A finishes -> finally -> global = None
    job B's next node calls _ctx() -> None -> "context not initialized"

Worse than a crash, B could also have run several nodes against **A's**
context — A's principal, A's project — before A cleared it.

This is not theoretical. It failed 11 of 14 papers in a single research run,
because a research run is precisely the concurrent case: ingest fans out, and
whichever job finishes first breaks every job still in flight. Ingesting the
same papers one at a time works perfectly, which is why it shipped.

A `ContextVar` is per-task: each asyncio task sees only what it set.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = REPO_ROOT / "packages"


def _workflow_modules() -> list[pathlib.Path]:
    """Every module holding a workflow context — found, not listed.

    A hardcoded list would silently stop covering a workflow added later, which
    is the same shape of bug as the one under test.
    """
    return sorted(
        p
        for p in PACKAGES.rglob("*.py")
        if "context not initialized" in p.read_text(encoding="utf-8")
    )


def test_workflow_modules_were_found() -> None:
    """Guard the guard: an empty sweep passes vacuously forever."""
    mods = _workflow_modules()
    assert len(mods) >= 5, f"only found {len(mods)} workflow modules; the sweep is not working"


@pytest.mark.parametrize("path", _workflow_modules(), ids=lambda p: p.stem)
def test_context_is_task_local_not_a_module_global(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # A `global _active_ctx` statement is the exact construct that breaks.
    globals_declared = [
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
        if "ctx" in name.lower()
    ]
    assert not globals_declared, (
        f"{path.name} rebinds {globals_declared} as a module global. arq runs "
        f"jobs concurrently in one event loop, so one job's context leaks into "
        f"another's and the first to finish clears it for everyone still "
        f"running. Use a ContextVar."
    )
    assert "ContextVar" in source, (
        f"{path.name} holds its workflow context outside a ContextVar; "
        f"concurrent runs will interfere"
    )


class TestContextVarActuallyIsolates:
    """The property itself, exercised — not just the spelling in the source."""

    @staticmethod
    async def _run(workflow_ctx: str, hold: float) -> str:
        from aleph_wiki.agent.workflow import _active_ctx_var, _ctx

        token = _active_ctx_var.set(workflow_ctx)  # type: ignore[arg-type]
        try:
            # Yield long enough for the other task to set (and clear) its own.
            await asyncio.sleep(hold)
            return str(_ctx())
        finally:
            _active_ctx_var.reset(token)

    @pytest.mark.asyncio
    async def test_interleaved_runs_keep_their_own_context(self) -> None:
        """The exact interleaving that broke: B finishes and clears while A waits."""
        slow, fast = await asyncio.gather(
            self._run("run-A", hold=0.05),
            self._run("run-B", hold=0.0),
        )
        assert (slow, fast) == ("run-A", "run-B"), (
            f"contexts crossed: the long-running task saw {slow!r}. With a module "
            f"global the fast task's cleanup would have left it None or 'run-B'."
        )

    @pytest.mark.asyncio
    async def test_many_concurrent_runs_do_not_interfere(self) -> None:
        results = await asyncio.gather(
            *[self._run(f"run-{i}", hold=(i % 5) / 100) for i in range(24)]
        )
        assert results == [f"run-{i}" for i in range(24)]

    @pytest.mark.asyncio
    async def test_context_is_absent_outside_a_run(self) -> None:
        """Leaking a context past its run would be the opposite failure."""
        from aleph_wiki.agent.workflow import _ctx

        await self._run("run-X", hold=0.0)
        with pytest.raises(RuntimeError, match="not initialized"):
            _ctx()
