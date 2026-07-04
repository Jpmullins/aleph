"""Loop bounds: plateau cutoff, iteration cap, total-source cap, routing."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from aleph_research.research_workflow import (
    ResearchLimits,
    _active_ctx_var,
    _node_reflect,
    route_after_reflect,
    should_stop,
)

_LIMITS = ResearchLimits(max_iterations=3, max_sources_per_iter=6, max_total_sources=15)


class TestShouldStop:
    def test_plateau_stops_even_on_first_iteration(self) -> None:
        assert should_stop(iteration=0, last_ingested_count=0, total_ingested=0, limits=_LIMITS)

    def test_continues_under_all_bounds(self) -> None:
        assert not should_stop(iteration=0, last_ingested_count=4, total_ingested=4, limits=_LIMITS)

    def test_iteration_bound(self) -> None:
        assert should_stop(iteration=2, last_ingested_count=4, total_ingested=8, limits=_LIMITS)

    def test_shallow_stops_after_one_iteration(self) -> None:
        shallow = ResearchLimits(max_iterations=1, max_sources_per_iter=6, max_total_sources=15)
        assert should_stop(iteration=0, last_ingested_count=4, total_ingested=4, limits=shallow)

    def test_total_source_cap(self) -> None:
        assert should_stop(iteration=0, last_ingested_count=6, total_ingested=15, limits=_LIMITS)


class TestRouting:
    def test_done_routes_to_compose(self) -> None:
        assert route_after_reflect({"research_done": True}) == "compose"  # type: ignore[typeddict-item]

    def test_not_done_routes_to_search(self) -> None:
        assert route_after_reflect({"research_done": False}) == "search"  # type: ignore[typeddict-item]

    def test_missing_flag_defaults_to_compose(self) -> None:
        assert route_after_reflect({}) == "compose"  # type: ignore[typeddict-item]


class _ExplodingLLM:
    """An LLM client that fails the test if the loop consults it."""

    async def chat(self, **_kwargs: Any) -> Any:
        raise AssertionError("LLM must not be called on the plateau path")


class _Anything:
    session_maker: Any = None


async def test_plateau_stops_loop_without_llm_call() -> None:
    """An iteration that ingested 0 new sources ends the loop before reflect's LLM."""
    from aleph_research.research_workflow import _Ctx

    ctx = _Ctx(
        session_maker=None,  # type: ignore[arg-type]  # with_phase no-ops without agent_run_id
        litellm=_ExplodingLLM(),  # type: ignore[arg-type]
        principal=None,  # type: ignore[arg-type]
        scholar=None,  # type: ignore[arg-type]
        asset_store=None,  # type: ignore[arg-type]
        tools_by_kind={},
        profile_bindings={},
        limits=_LIMITS,
        agent_token_secret="s",
        enqueue=None,  # type: ignore[arg-type]
    )
    token = _active_ctx_var.set(ctx)
    try:
        out = await _node_reflect(
            {
                "agent_run_id": None,
                "project_id": uuid4(),
                "topic": "T",
                "iteration": 0,
                "ingested": [],
                "last_ingested_count": 0,
            }
        )
    finally:
        _active_ctx_var.reset(token)
    assert out == {"research_done": True}
    assert route_after_reflect({**out}) == "compose"  # type: ignore[typeddict-item]


def test_graph_constructs_and_compiles() -> None:
    """StateGraph resolves the state TypedDict's annotations at construction
    (get_type_hints) — a TYPE_CHECKING-only import of a state type crashes
    at runtime even though pyright is happy. Regression for the live
    `name 'UUID' is not defined` failure."""
    from unittest.mock import MagicMock

    from aleph_research.research_workflow import ResearchLimits, ResearchWorkflow

    wf = ResearchWorkflow(
        session_maker=MagicMock(),
        litellm=MagicMock(),
        principal=MagicMock(),
        scholar=MagicMock(),
        asset_store=MagicMock(),
        tools=[],
        profile_bindings={},
        limits=ResearchLimits(max_iterations=1, max_sources_per_iter=2, max_total_sources=3),
        agent_token_secret="x" * 32,
        enqueue=MagicMock(),
    )
    assert wf is not None
