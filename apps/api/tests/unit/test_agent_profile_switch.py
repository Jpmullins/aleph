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
from aleph_models.endpoints import SOURCE_ROW, SOURCE_SETTINGS, ResolvedEndpoint


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


def _base_urls(agent: Any) -> set[str]:
    """Every gateway the graph's seven models point at. One, if it is correct."""
    kwargs = agent.graph.aleph_captured
    models = [kwargs["model"], *(sub["model"] for sub in kwargs["subagents"])]
    return {str(m.openai_api_base) for m in models}


def _endpoint_loader(endpoints: dict[UUID | None, ResolvedEndpoint]) -> Any:
    """Stand in for `endpoint_for_project`'s single SELECT."""

    async def load(project_id: UUID | None) -> ResolvedEndpoint:
        return endpoints[project_id]

    return load


def _row(base_url: str, api_key: str = "sk-row") -> ResolvedEndpoint:
    """A `gateway_endpoints` row, as `GatewayEndpointService.resolve` returns it."""
    return ResolvedEndpoint(
        base_url=base_url,
        api_key=api_key,
        name="primary",
        endpoint_id=uuid4(),
        source=SOURCE_ROW,
    )


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


# ---------------------------------------------------------------------------
# c5: the endpoint half of the resolution — WS-MEP-4's rows reaching the agent
# ---------------------------------------------------------------------------


async def test_two_projects_on_two_gateways_build_against_two_gateways(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """MEP-4's rows, reaching the assistant. Before this they did not.

    `resolve_agent` computed `_openai_base_url(settings.litellm_base_url)` for
    every project, so a project pointed at its own gateway had a row that read
    back correctly on the settings screen and an assistant still talking to the
    deployment default. The endpoint component of the cache signature was a
    constant, which the signature's own docstring warns is "the whole of
    WS-MEP-4 made inert".
    """
    a, b = uuid4(), uuid4()
    resolve = assistant_agent_resolver(
        settings=_settings("http://boot-gateway.invalid"),
        store=None,
        load_bindings=_loader({a: ALPHA, b: ALPHA}),
        load_endpoint=_endpoint_loader(
            {a: _row("http://gw-a.invalid"), b: _row("http://gw-b.invalid")}
        ),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    assert _base_urls(await resolve(a)) == {"http://gw-a.invalid/v1"}
    assert _base_urls(await resolve(b)) == {"http://gw-b.invalid/v1"}
    # Identical bindings, different endpoints: two graphs. One graph here would
    # mean the cache key ignored the endpoint, and project B would be answered
    # by a graph whose seven models all point at A's gateway.
    assert len(captured_graphs) == 2
    # And neither of them fell back to the boot setting.
    assert all("boot-gateway" not in url for g in captured_graphs for url in _urls_of(g))


def _urls_of(captured: dict[str, Any]) -> set[str]:
    models = [captured["model"], *(sub["model"] for sub in captured["subagents"])]
    return {str(m.openai_api_base) for m in models}


async def test_the_credential_travels_with_the_url(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """A resolved endpoint reached with the deployment's key is a 401.

    Both halves come off one row, and a refactor that threaded the URL and left
    the key on Settings would pass every base-url assertion in this file while
    failing on the first real call — against somebody else's quota if the two
    gateways happen to share a provider.
    """
    project = uuid4()
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader({project: ALPHA}),
        load_endpoint=_endpoint_loader(
            {project: _row("http://gw-a.invalid", api_key="sk-project-a")}
        ),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    captured = (await resolve(project)).graph.aleph_captured
    models = [captured["model"], *(sub["model"] for sub in captured["subagents"])]
    keys = {m.openai_api_key.get_secret_value() for m in models}
    assert keys == {"sk-project-a"}


async def test_repointing_a_project_takes_effect_on_the_next_turn(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """The endpoint's version of c2. No restart, no cache clear."""
    project = uuid4()
    endpoints = {project: _row("http://gw-a.invalid")}
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader({project: ALPHA}),
        load_endpoint=_endpoint_loader(endpoints),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    assert _base_urls(await resolve(project)) == {"http://gw-a.invalid/v1"}
    endpoints[project] = _row("http://gw-b.invalid")
    assert _base_urls(await resolve(project)) == {"http://gw-b.invalid/v1"}


async def test_rotating_a_key_at_an_unchanged_url_rebuilds_the_graph(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """ "It worked yesterday", prevented.

    The URL and the bindings are identical, so a signature over those two alone
    is a cache HIT and the seven models keep the revoked key until something
    happens to evict the entry. `ProjectGatewayCatalogs` keys on a digest of
    the key for exactly this reason; the graph cache has to as well.
    """
    project = uuid4()
    endpoints = {project: _row("http://gw-a.invalid", api_key="sk-old")}
    resolve = assistant_agent_resolver(
        settings=_settings(),
        store=None,
        load_bindings=_loader({project: ALPHA}),
        load_endpoint=_endpoint_loader(endpoints),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    first = await resolve(project)
    endpoints[project] = _row("http://gw-a.invalid", api_key="sk-rotated")
    second = await resolve(project)

    assert second is not first, "a rotated key reused the graph built with the old one"
    captured = second.graph.aleph_captured
    assert captured["model"].openai_api_key.get_secret_value() == "sk-rotated"


async def test_a_project_with_no_row_still_uses_the_deployment_gateway(
    captured_graphs: list[dict[str, Any]],
) -> None:
    """Adoption without a flag day, and the reason the fallback is not a guess.

    `settings_endpoint` is the same value `GatewayEndpointService.resolve`
    returns for a project with no row, so the un-configured case goes down one
    code path rather than two that can disagree.
    """
    project = uuid4()
    resolve = assistant_agent_resolver(
        settings=_settings("http://boot-gateway.invalid"),
        store=None,
        load_bindings=_loader({project: ALPHA}),
        cache=BoundedGraphCache(AGENT_GRAPH_CACHE_MAX),
    )

    # No `load_endpoint`: production's `endpoint_for_project` runs, and with no
    # `session_maker` bound on `_runtime` it returns the deployment default.
    assert copilot_agent._runtime.get("session_maker") is None
    assert _base_urls(await resolve(project)) == {"http://boot-gateway.invalid/v1"}


async def test_the_resolution_reports_which_endpoint_answered() -> None:
    """`row` and `settings` are different facts and must stay distinguishable.

    They bill identically. Without this, "the assistant is on the wrong
    gateway" and "nobody has configured one" look the same from outside, which
    is the diagnosis problem `AgentResolution` exists as a value to solve.
    """
    from aleph_api.copilot_agent import resolve_agent

    project = uuid4()
    row = await resolve_agent(
        project,
        settings=_settings(),
        load_bindings=_loader({project: ALPHA}),
        load_endpoint=_endpoint_loader({project: _row("http://gw-a.invalid")}),
    )
    assert row.endpoint_source == SOURCE_ROW
    assert row.endpoint_id is not None
    assert row.endpoint == "http://gw-a.invalid/v1"

    fell_through = await resolve_agent(
        project,
        settings=_settings("http://boot-gateway.invalid"),
        load_bindings=_loader({project: ALPHA}),
    )
    assert fell_through.endpoint_source == SOURCE_SETTINGS
    assert fell_through.endpoint_id is None


def test_the_signature_never_contains_the_key_itself() -> None:
    """A cache key is logged and would be reported by a diagnostics route.

    The key has to be IN the signature — a rotation must invalidate it — and it
    must not be in it verbatim. Digesting is what makes both true at once.
    """
    secret = "sk-a-real-looking-credential-0123456789"
    signed = agent_resolution_signature(
        endpoint="http://gw-a.invalid/v1", bindings=ALPHA, api_key=secret
    )
    assert secret not in signed
    assert signed != agent_resolution_signature(
        endpoint="http://gw-a.invalid/v1", bindings=ALPHA, api_key="sk-different"
    )


def test_the_resolution_does_not_print_its_credential() -> None:
    """`repr` is where a secret leaks without anybody choosing to log it.

    An `AgentResolution` reaches structlog events and tracebacks, and MEP-6's
    iterate note proposes serialising one over HTTP.
    """
    resolution = AgentResolution(
        project_id=uuid4(),
        endpoint="http://gw-a.invalid/v1",
        bindings=ALPHA,
        signature="deadbeef",
        endpoint_source=SOURCE_ROW,
        api_key="sk-must-not-appear",
    )
    assert "sk-must-not-appear" not in repr(resolution)
    assert "gw-a.invalid" in repr(resolution)
    # Still reachable by name, because the model builders need it.
    assert resolution.agent_endpoint.api_key == "sk-must-not-appear"
    assert "sk-must-not-appear" not in repr(resolution.agent_endpoint)
