"""A profile registered in a file changes what the model is SENT. WS-MEP-7 c3/c4.

`apps/api/tests/unit/test_harness_profiles.py` checks the file: what a key may
look like, and what happens when the YAML is wrong. It deliberately asserts
nothing about effect, because a registry read by nobody is exactly the
producer-with-no-consumer defect this repository keeps finding.

This file is the consumer, measured where it cannot be faked: one real assistant
turn is driven against a `FakeGateway`, and the assertions read the request body
the gateway recorded — the `tools` array the model was actually offered, and the
system prompt it was actually sent. A profile that registers cleanly and reaches
no model passes every unit test above and fails every test here.

**Why the whole graph and not `create_deep_agent` with two toy tools.** The
claim under test is Aleph's, not deepagents': that `build_assistant_deep_agent`
registers profiles BEFORE it builds, and that the model it built is the one the
profile is keyed to. Compiling the real orchestrator — thirty tools, seven
models, the middleware stack — is the only way the ordering can be observed,
and it is the ordering that fails silently: a profile registered after
`create_deep_agent` returns affects the next graph, so the symptom is a setting
that "starts working after a restart".

The store is in-memory and the transport is the fake; nothing else is
substituted. No database is touched, but the marker stays `integration` because
a full graph compile plus a turn is not a unit test's cost.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from aleph_api.harness_profiles import (
    HARNESS_PROFILES_ENV,
    reset_harness_profile_registration,
)
from aleph_models.limiter import reset_limiters
from aleph_models.testing import FakeGateway, FakeModel, GatewayConfig

pytestmark = pytest.mark.integration

SUFFIX = "SENTINEL: answer in one step, and never delegate."

#: Named, not counted. "the tool set is smaller" is satisfiable by removing any
#: three tools, including three that mattered; the criterion asks which.
WITHHELD_FROM_A_SMALL_MODEL = ("start_background_task", "author_plugin", "compose_dossier")


@pytest.fixture(autouse=True)
def _clean_process_state() -> Iterator[None]:
    reset_limiters()
    reset_harness_profile_registration()
    yield
    reset_limiters()
    reset_harness_profile_registration()


@pytest.fixture
def models() -> tuple[str, str]:
    """Two model ids that exist only for THIS test, and why that is load bearing.

    deepagents' harness-profile registry is process-global and has no removal
    API, so a module-level id lets the first test that registers it decide the
    result of every later one. With shared ids the ordering criterion below
    could not fail: moving `ensure_harness_profiles_registered()` to AFTER
    `create_deep_agent` still leaves the profile registered by the time the
    NEXT test — or the next turn in the same test — builds its graph, and every
    assertion stayed green against a mutation that broke the feature. Measured:
    21 passed. Fresh ids per test are the only isolation available.
    """
    token = uuid.uuid4().hex[:8]
    return f"vllm-local-small-{token}", f"vllm-local-big-{token}"


@pytest.fixture
def fake(models: tuple[str, str]) -> FakeGateway:
    small, big = models
    return FakeGateway(
        GatewayConfig.well_behaved(
            models=(
                FakeModel(id=small, mode="chat", supports_function_calling=True),
                FakeModel(id=big, mode="chat", supports_function_calling=True),
            ),
            chat_reply="ok",
        )
    )


@pytest.fixture
def agent_transport(monkeypatch: pytest.MonkeyPatch, fake: FakeGateway) -> None:
    """`ChatOpenAI` builds its own client; `_gateway_chat_model` hands it this one."""
    from aleph_models import limiter as limiter_mod

    monkeypatch.setattr(
        limiter_mod,
        "shared_gateway_client",
        lambda base_url, **_kw: fake.client(base_url=base_url),
    )


def _settings(fake: FakeGateway) -> SimpleNamespace:
    return SimpleNamespace(
        litellm_base_url=fake.base_url,
        insights_litellm_api_key=fake.api_key,
        aleph_agent_request_timeout_s=30.0,
    )


def _bindings(model: str) -> dict[str, Any]:
    return {cap: {"model": model, "provider": "litellm"} for cap in ("synthesis", "judge", "code")}


def _profile_file(tmp_path: Path, model: str) -> Path:
    path = tmp_path / "harness-profiles.yaml"
    excluded = "\n".join(f"      - {name}" for name in WITHHELD_FROM_A_SMALL_MODEL)
    path.write_text(
        f'profiles:\n  "openai:{model}":\n'
        f"    system_prompt_suffix: |\n      {SUFFIX}\n"
        f"    excluded_tools:\n{excluded}\n",
        encoding="utf-8",
    )
    return path


async def _one_turn(model: str, fake: FakeGateway) -> None:
    """Compile the real assistant for `model` and take exactly one turn."""
    from aleph_api import copilot_agent

    graph = copilot_agent.build_assistant_deep_agent(
        settings=_settings(fake),
        store=InMemoryStore(),
        checkpointer=None,
        bindings=_bindings(model),
    )
    await graph.ainvoke(
        {"messages": [{"role": "user", "content": "hello"}]},
        {"configurable": {"thread_id": f"t-{uuid.uuid4().hex}"}},
    )


def _first_chat(fake: FakeGateway) -> dict[str, Any]:
    bodies = [
        r.body for r in fake.requests if r.path.endswith("/chat/completions") and r.body is not None
    ]
    assert bodies, "the assistant never reached the gateway"
    return bodies[0]


def _tool_names(body: dict[str, Any]) -> set[str]:
    tools = body.get("tools") or []
    return {str(t["function"]["name"]) for t in tools}


# ---------------------------------------------------------------------------
# c3 — a profiled model is offered a smaller tool set, by name
# ---------------------------------------------------------------------------


async def test_a_profiled_model_is_offered_fewer_tools_and_the_names_are_the_declared_ones(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake: FakeGateway,
    models: tuple[str, str],
    agent_transport: None,
) -> None:
    """Measured on the wire, and BY NAME.

    "smaller" alone is satisfiable by removing any three tools, including three
    that mattered. The assertion is set difference: exactly the declared names
    are gone and nothing else moved.

    The PROFILED graph is built FIRST, deliberately. Building the unprofiled one
    first would call `ensure_harness_profiles_registered()` on the way, so a
    build that registered its profiles too late would still have them in place
    by the time the second graph was assembled — and this test would pass
    against exactly the defect it exists to catch.
    """
    small, big = models
    monkeypatch.setenv(HARNESS_PROFILES_ENV, str(_profile_file(tmp_path, small)))

    await _one_turn(small, fake)
    profiled = _tool_names(_first_chat(fake))
    assert profiled, "the profiled model was offered no tools at all"

    fake.reset()
    await _one_turn(big, fake)
    unprofiled = _tool_names(_first_chat(fake))

    assert unprofiled - profiled == set(WITHHELD_FROM_A_SMALL_MODEL), (
        f"expected exactly {sorted(WITHHELD_FROM_A_SMALL_MODEL)} withheld; "
        f"removed {sorted(unprofiled - profiled)}"
    )
    assert profiled - unprofiled == set(), "the profile ADDED tools"
    assert len(profiled) == len(unprofiled) - len(WITHHELD_FROM_A_SMALL_MODEL)


async def test_the_profile_does_not_touch_a_model_it_is_not_keyed_to(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake: FakeGateway,
    models: tuple[str, str],
    agent_transport: None,
) -> None:
    """The blast radius, asserted rather than assumed.

    This is the failure a bare provider key produces: both models are reached
    through the same gateway and resolve to provider `openai`, so a profile
    registered under `"openai"` would take these three tools away from the
    frontier model too, and nothing would report it. The refusal that prevents
    it lives in `harness_profiles.parse_profiles`; this is what it protects.
    """
    from deepagents.profiles.harness.harness_profiles import _get_harness_profile

    small, big = models
    monkeypatch.setenv(HARNESS_PROFILES_ENV, str(_profile_file(tmp_path, small)))

    await _one_turn(big, fake)
    body = _first_chat(fake)
    # The profile really is live — otherwise "it did not affect the big model"
    # would be true of a registration that never happened, and this test would
    # be green with the feature removed entirely.
    assert _get_harness_profile(f"openai:{small}") is not None
    assert set(WITHHELD_FROM_A_SMALL_MODEL) <= _tool_names(body)
    assert SUFFIX not in str(body["messages"][0]["content"])


# ---------------------------------------------------------------------------
# c4 — registration provably precedes graph construction
# ---------------------------------------------------------------------------


async def test_the_sentinel_suffix_is_in_the_prompt_the_model_receives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake: FakeGateway,
    models: tuple[str, str],
    agent_transport: None,
) -> None:
    """The ordering criterion, as a property of the first request sent.

    `create_deep_agent` reads the registry once, while it assembles the prompt.
    Move `ensure_harness_profiles_registered()` in
    `build_assistant_deep_agent` to AFTER the `create_deep_agent` call and this
    assertion fails while every unit test in
    `apps/api/tests/unit/test_harness_profiles.py` stays green — which is
    exactly why the criterion is stated as an ordering and checked here.
    """
    small, _big = models
    monkeypatch.setenv(HARNESS_PROFILES_ENV, str(_profile_file(tmp_path, small)))

    # Exactly ONE graph is built here, and that is the whole test: the registry
    # is read while `create_deep_agent` assembles the prompt, so a registration
    # that happens after it returns cannot reach this turn. A second build in
    # this test would supply the profile the first one missed.
    await _one_turn(small, fake)
    system = str(_first_chat(fake)["messages"][0]["content"])
    assert SUFFIX in system
    # A suffix, not a replacement: Aleph's own instructions must survive it.
    assert "You are Aleph" in system
    assert system.index("You are Aleph") < system.index(SUFFIX)


async def test_a_broken_profile_file_stops_the_build_instead_of_ignoring_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake: FakeGateway,
    models: tuple[str, str],
    agent_transport: None,
) -> None:
    """c5's consequence. A small model handed the full prompt does not crash —
    it just answers badly, so silently skipping a broken file is the worst
    possible outcome and the one a `try/except` would produce."""
    from aleph_core.errors import ValidationFailed

    broken = tmp_path / "harness-profiles.yaml"
    broken.write_text('profiles:\n  "openai:m":\n    system_prompt_sufix: "typo"\n', "utf-8")
    monkeypatch.setenv(HARNESS_PROFILES_ENV, str(broken))

    with pytest.raises(ValidationFailed) as raised:
        await _one_turn(models[0], fake)
    assert str(broken) in str(raised.value)
    assert fake.request_count == 0, "a turn was taken against a misconfigured harness"
