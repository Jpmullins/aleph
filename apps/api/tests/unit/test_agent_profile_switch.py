"""The assistant is built from the project's profile, per turn. WS-MEP-6.

What was true before this file existed: the API read ONE globally named model
profile at boot, compiled one deep agent from it, and served that graph to every
project until the process restarted. Changing a project's models in Settings had
no effect on the assistant at all — and `set_model_profile` returned "New
LLM/agent calls use that profile's models", which was not true of the system. A
control that reports an effect it does not have is worse than no control,
because it teaches the user to trust a false report.

These tests drive the real resolution path: `assistant_agent_resolver` ->
`resolve_agent` -> `agent_resolution_signature` -> `BoundedGraphCache` ->
`build_assistant_deep_agent` -> `use_agent_bindings` -> all seven
`_gateway_chat_model` calls (the orchestrator and the six subagents, which build
their own models from their own modules). Only two things are substituted: the
bindings loader, which stands in for one indexed SELECT, and
`deepagents.create_deep_agent`, captured so the built models can be read back —
the same technique `test_agent_skill_wiring.py` uses. Everything between them is
production code.
"""

from __future__ import annotations

import gc
import weakref
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import deepagents
import pytest

from aleph_api import copilot_agent
from aleph_api.copilot_agent import (
    AGENT_GRAPH_CACHE_MAX,
    AgentResolution,
    BoundedGraphCache,
    agent_resolution_signature,
    assistant_agent_resolver,
    switched_profile_message,
)


def _profile(model: str) -> dict[str, Any]:
    """A complete profile: the three capabilities the seven agent models need.

    Deliberately complete. A profile missing `judge` or `code` does not fall
    back to the orchestrator's model any more — it raises `NoModelBound` when
    the reviewer or the viz-builder subagent is constructed, which is a real
    property of the system and not something these tests should route around.
    """
    return {
        "synthesis": {"model": model, "provider": "litellm"},
        "judge": {"model": model, "provider": "litellm"},
        "code": {"model": model, "provider": "litellm"},
    }


ALPHA = _profile("alpha-model")
BETA = _profile("beta-model")


def _settings(base_url: str = "http://gateway.invalid") -> Any:
    return SimpleNamespace(
        litellm_base_url=base_url,
        insights_litellm_api_key="sk-not-a-real-key",
        aleph_agent_request_timeout_s=120.0,
    )


@pytest.fixture
def captured_graphs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what `create_deep_agent` is handed, and hand back a stand-in.

    `nodes` is present because `LangGraphAGUIAgent.__init__` reads it; nothing
    else about a compiled graph is exercised here, and compiling fifty real ones
    would test LangGraph rather than Aleph.
    """
    seen: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(nodes={}, aleph_captured=kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", _capture)
    return seen


def _loader(profiles: dict[UUID | None, dict[str, Any] | None]) -> Any:
    """Stand in for `bindings_for_project`'s single SELECT."""

    async def load(project_id: UUID | None) -> Any:
        return profiles.get(project_id)

    return load


def _model_names(agent: Any) -> list[str]:
    """Every model the graph was built with: the orchestrator's, then the six."""
    kwargs = agent.graph.aleph_captured
    names = [kwargs["model"].model_name]
    names.extend(sub["model"].model_name for sub in kwargs["subagents"])
    return names


# ---------------------------------------------------------------------------
# c1 / c2: two projects, two models — and a rebind that lands without a restart
# ---------------------------------------------------------------------------


async def test_two_projects_bound_to_different_models_use_different_models(
    captured_graphs: list[dict[str, Any]],
) -> None:
    a, b = uuid4(), uuid4()
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader({a: ALPHA, b: BETA}),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    assert _model_names(await resolve(a))[0] == "alpha-model"
    assert _model_names(await resolve(b))[0] == "beta-model"
    assert len(captured_graphs) == 2, "the second project reused the first project's graph"


async def test_every_subagent_comes_from_the_same_profile(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """Seven models, one profile. Six of them are built by other modules.

    The bindings travel by ContextVar precisely so `subagents/*.py` need no
    change; the failure this guards is a graph whose orchestrator honours the
    project and whose subagents quietly do not.
    """
    project = uuid4()
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader(
            {
                project: {
                    "synthesis": {"model": "alpha-model"},
                    "judge": {"model": "alpha-judge"},
                    "code": {"model": "alpha-code"},
                }
            }
        ),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    names = _model_names(await resolve(project))
    assert len(names) == 7, f"expected orchestrator + six subagents, got {names}"
    assert set(names) == {"alpha-model", "alpha-judge", "alpha-code"}


async def test_rebinding_takes_effect_on_the_next_turn_without_a_restart(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """The criterion PATCH /model-profile has to satisfy.

    The dict is mutated the way `switch_project_profile` mutates the row: the
    project's bindings are replaced with a template's. Nothing is restarted and
    no cache is cleared.
    """
    project = uuid4()
    profiles: dict[UUID | None, dict[str, Any] | None] = {project: ALPHA}
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader(profiles),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    assert _model_names(await resolve(project))[0] == "alpha-model"
    profiles[project] = BETA
    assert _model_names(await resolve(project))[0] == "beta-model"


async def test_an_unchanged_binding_reuses_the_compiled_graph(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """Two projects on one endpoint with identical bindings compile once.

    Without this the resolver is a per-request `create_deep_agent`, which is the
    other way to get this wrong: correct models, and a compile on every turn.
    """
    a, b = uuid4(), uuid4()
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader({a: dict(ALPHA), b: dict(ALPHA)}),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    first = await resolve(a)
    assert await resolve(b) is first
    assert await resolve(a) is first
    assert len(captured_graphs) == 1


# ---------------------------------------------------------------------------
# The signature: both halves, and each one's own failure
# ---------------------------------------------------------------------------


def test_the_endpoint_is_part_of_the_signature() -> None:
    """Drop it and two projects on different gateways share one graph."""
    assert agent_resolution_signature(
        endpoint="http://one.invalid/v1", bindings=ALPHA
    ) != agent_resolution_signature(endpoint="http://two.invalid/v1", bindings=ALPHA)


def test_the_bindings_are_part_of_the_signature() -> None:
    """Drop them and a project that rebinds keeps the graph it had."""
    assert agent_resolution_signature(
        endpoint="http://one.invalid/v1", bindings=ALPHA
    ) != agent_resolution_signature(endpoint="http://one.invalid/v1", bindings=BETA)


def test_the_signature_does_not_depend_on_key_order() -> None:
    """`bindings_jsonb` comes back from Postgres in no guaranteed order.

    An order-sensitive signature would recompile the same profile on alternate
    turns and evict everything else doing it — a leak that looks like a cache.
    """
    one = {"synthesis": {"model": "m", "provider": "litellm"}, "judge": {"model": "j"}}
    two = {"judge": {"model": "j"}, "synthesis": {"provider": "litellm", "model": "m"}}
    assert agent_resolution_signature(endpoint="e", bindings=one) == agent_resolution_signature(
        endpoint="e", bindings=two
    )


async def test_two_endpoints_do_not_share_a_graph(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """The signature check above, driven through the resolver rather than asserted."""
    project = uuid4()
    cache = BoundedGraphCache(AGENT_GRAPH_CACHE_MAX)
    one = assistant_agent_resolver(
        settings=_settings("http://one.invalid"),
        store=None,
        load_bindings=_loader({project: ALPHA}),
        cache=cache,
    )
    two = assistant_agent_resolver(
        settings=_settings("http://two.invalid"),
        store=None,
        load_bindings=_loader({project: ALPHA}),
        cache=cache,
    )

    assert await one(project) is not await two(project)
    assert len(cache) == 2


# ---------------------------------------------------------------------------
# c3: the cache is bounded, and what it drops is really gone
# ---------------------------------------------------------------------------


async def test_fifty_distinct_profiles_leave_eight_graphs(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """Correction #10's replacement criterion, both halves.

    The cache key is derived from user-controlled state — a project's bindings
    change whenever somebody clicks Save — so an unbounded map keyed on it is a
    memory leak with a UI attached. `8` is written literally rather than read
    from `AGENT_GRAPH_CACHE_MAX`: a test that derives its expectation from the
    constant it is checking passes for any value of that constant.
    """
    cache = BoundedGraphCache(AGENT_GRAPH_CACHE_MAX)
    profiles: dict[UUID | None, dict[str, Any] | None] = {}
    resolve = assistant_agent_resolver(
        settings=_settings(), store=None, load_bindings=_loader(profiles), cache=cache
    )

    alive: list[weakref.ref[Any]] = []
    for index in range(50):
        project = uuid4()
        profiles[project] = _profile(f"model-{index}")
        alive.append(weakref.ref(await resolve(project)))

    assert len(captured_graphs) == 50, "distinct profiles must not share a graph"
    assert len(cache) == 8, f"the cache holds {len(cache)} graphs, not 8"

    gc.collect()
    dead = [ref for ref in alive if ref() is None]
    assert len(dead) == 42, (
        f"{50 - len(dead)} of 50 graphs are still reachable after eviction — "
        "the bound is on the dict, not on the memory."
    )


def test_a_cache_that_holds_nothing_is_refused() -> None:
    """`BoundedGraphCache(0)` would recompile seven models on every turn."""
    with pytest.raises(ValueError, match="rebuild every turn"):
        BoundedGraphCache(0)


def test_a_failed_build_leaves_no_entry_behind() -> None:
    """A half-built graph in the cache would be served to every later turn."""
    cache = BoundedGraphCache(4)

    def _explode() -> Any:
        msg = "no model is bound"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        cache.get_or_build("sig", _explode)
    assert len(cache) == 0
    assert cache.get_or_build("sig", lambda: "built later") == "built later"


def test_the_cache_evicts_least_recently_used_not_least_recently_built() -> None:
    """A project that keeps chatting must not be evicted by one that stopped."""
    cache = BoundedGraphCache(2)
    cache.get_or_build("a", lambda: "A")
    cache.get_or_build("b", lambda: "B")
    cache.get_or_build("a", lambda: "rebuilt-A")  # a hit; refreshes a's recency
    cache.get_or_build("c", lambda: "C")

    assert cache.keys() == ["a", "c"]


# ---------------------------------------------------------------------------
# c6: the sentence the tool returns is true of the system
# ---------------------------------------------------------------------------


def test_the_switch_message_names_the_model_the_next_turn_resolves() -> None:
    """The claim and the resolver are the same computation, not two prose copies.

    The old sentence — "New LLM/agent calls use that profile's models" — asserted
    an effect nothing produced. This one names a model, and the name comes from
    `AgentResolution.model_for`, which is what the graph's orchestrator model is
    built by.
    """
    message = switched_profile_message("some-template", ALPHA)
    resolved = AgentResolution(project_id=None, endpoint="", bindings=ALPHA, signature="")

    assert resolved.model_for("synthesis") == "alpha-model"
    assert "alpha-model" in message
    assert "next turn" in message


async def test_the_message_agrees_with_the_graph_that_gets_built(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """End to end: what the user is told, and what the next turn is built from."""
    project = uuid4()
    profiles: dict[UUID | None, dict[str, Any] | None] = {project: ALPHA}
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader(profiles),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    profiles[project] = BETA
    message = switched_profile_message("some-template", BETA)
    orchestrator = _model_names(await resolve(project))[0]

    assert orchestrator in message, f"the tool said {message!r}, the graph uses {orchestrator!r}"


def test_the_message_refuses_to_claim_an_effect_it_cannot_have() -> None:
    """A profile binding no synthesis model does not get a reassuring sentence."""
    message = switched_profile_message("empty-template", {"embedding": {"model": "e"}})
    assert "will fail" in message
    assert "Nothing was guessed." in message


def test_the_tool_names_no_model_profile_template() -> None:
    """`aleph-dev` / `aleph-production` are rows in a database, not a shipped list.

    Naming them in the tool's own docstring was a committed claim about what a
    template contains on somebody else's gateway.
    """
    doc = copilot_agent.set_model_profile.description or ""
    assert "aleph-dev" not in doc
    assert "aleph-production" not in doc
    assert "empty string" in doc, "the tool no longer tells the model how to LIST templates"


# ---------------------------------------------------------------------------
# The one SELECT: what it reads, and what it refuses to hide
# ---------------------------------------------------------------------------


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _bind_session_maker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(copilot_agent._runtime, "session_maker", _Session)


async def test_a_turn_with_no_project_gets_the_boot_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct caller with no project-prefixed thread id still gets an agent."""
    _bind_session_maker(monkeypatch)
    monkeypatch.setitem(copilot_agent._runtime, "agent_bindings", ALPHA)
    assert await copilot_agent.bindings_for_project(None) == ALPHA


async def test_a_project_with_no_profile_row_gets_the_boot_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_session_maker(monkeypatch)
    monkeypatch.setitem(copilot_agent._runtime, "agent_bindings", ALPHA)

    import aleph_db.repos.model_profile as repo

    async def _none(_session: object, _project_id: UUID) -> None:
        return None

    monkeypatch.setattr(repo, "get_project_profile", _none)
    assert await copilot_agent.bindings_for_project(uuid4()) == ALPHA


async def test_the_project_row_wins_over_the_boot_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the whole workstream in one assertion."""
    _bind_session_maker(monkeypatch)
    monkeypatch.setitem(copilot_agent._runtime, "agent_bindings", ALPHA)

    import aleph_db.repos.model_profile as repo

    async def _row(_session: object, _project_id: UUID) -> Any:
        return SimpleNamespace(bindings_jsonb=BETA)

    monkeypatch.setattr(repo, "get_project_profile", _row)
    assert await copilot_agent.bindings_for_project(uuid4()) == BETA


async def test_a_failed_read_is_not_silently_the_boot_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swallowing this would put the assistant on the wrong model and say nothing.

    That is the defect the workstream exists to remove, so the read raises and
    the AG-UI route turns it into a RUN_ERROR frame with a searchable id —
    `test_agent_endpoint_errors.py::test_a_resolution_failure_is_reported_as_run_error`.
    """
    _bind_session_maker(monkeypatch)
    monkeypatch.setitem(copilot_agent._runtime, "agent_bindings", ALPHA)

    import aleph_db.repos.model_profile as repo

    async def _explode(_session: object, _project_id: UUID) -> Any:
        msg = "connection refused"
        raise OSError(msg)

    monkeypatch.setattr(repo, "get_project_profile", _explode)
    with pytest.raises(OSError, match="connection refused"):
        await copilot_agent.bindings_for_project(uuid4())


async def test_the_production_resolver_uses_the_bounded_process_cache(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """Every other test here passes its own cache, so none of them checks this.

    The bound is only real if the resolver PRODUCTION builds — the one
    `copilotkit_endpoint` installs, with no `cache=` — is the bounded one. A
    default of `{}` would pass every test above and leak in the only process
    that matters.
    """
    cache = copilot_agent.agent_graph_cache()
    cache.clear()
    try:
        assert cache.max_entries == 8

        project = uuid4()
        resolve = assistant_agent_resolver(
            settings=_settings(), store=None, load_bindings=_loader({project: ALPHA})
        )
        await resolve(project)
        assert len(cache) == 1, "the resolver did not use the process-wide cache"
    finally:
        cache.clear()
