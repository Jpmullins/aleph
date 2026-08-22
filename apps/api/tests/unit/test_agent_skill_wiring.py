"""What `build_assistant_deep_agent` actually passes to deepagents.

These assertions exist because the obvious way to test WS-H1 does not work. A
test that imports `SKILL_SOURCES` and checks it has two entries stays green when
somebody edits the call site back to `skills=["/skills"]` — it tests the
constant, not the wiring, and the wiring is the entire feature. Same for the
permission rules and the backend routes.

So this patches `deepagents.create_deep_agent`, runs the real builder, and reads
the kwargs it was called with. Every mutation in WS-H1's Review step lands here:

  (a) drop `/skills/authored` from `skills=`  → test_both_skill_sources_are_passed
  (b) a constant namespace instead of the callable → test_authored_skills_are_project_scoped
  (c) allow after deny in the permission list → test_the_allow_precedes_the_deny
"""

from __future__ import annotations

from typing import Any

import pytest

from aleph_api.authored_skills import AUTHORED_PREFIX, AuthoredSkillsMiddleware


@pytest.fixture
def call(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The kwargs the production builder hands `create_deep_agent`."""
    import deepagents

    from aleph_api.settings import Settings

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", _capture)

    from aleph_api.copilot_agent import build_assistant_deep_agent

    build_assistant_deep_agent(settings=_settings_with_placeholders(Settings), store=None)
    return captured


def _settings_with_placeholders(cls: type) -> Any:
    """Settings with every required secret filled by an obvious placeholder.

    Derived from the model rather than listed, deliberately. Aleph's settings
    carry secrets that must have no default in production, and the list grows —
    hard-coding today's names here would make this file fail on an unrelated
    change and teach whoever hits it to stop trusting it. The subject under test
    is what the agent builder passes to deepagents; nothing here reaches a
    gateway or a cipher.
    """
    values = {
        # 64 chars: long enough for the minimum-length validators the secret
        # fields carry, and unmistakably not a key.
        name: "placeholder-not-a-real-secret-placeholder-not-a-real-secret-xxxxx"
        for name, field in cls.model_fields.items()
        if field.is_required()
    }
    return cls(**values)


def test_both_skill_sources_are_passed(call: dict[str, Any]) -> None:
    """One source is not enough, and the failure is silent.

    `_list_skills_with_errors` runs per source path, so `skills=["/skills"]`
    returns the four bundled skills and never the store's — however the routes
    are configured. The agent writes a skill, then never mentions it again,
    which reads as the model being unhelpful rather than as a config error.
    """
    assert call["skills"] == ["/skills", "/skills/authored"]


def test_the_allow_precedes_the_deny(call: dict[str, Any]) -> None:
    """Order is the mechanism, not a style preference.

    `_check_fs_permission` is first-match-wins. An allow placed after the deny
    is inert: every authored write is refused and nothing reports it.
    """
    rules = call["permissions"]
    modes = [(r.mode, tuple(r.paths)) for r in rules]
    assert modes[0] == ("allow", (f"{AUTHORED_PREFIX}**",)), modes
    assert modes[1] == ("deny", ("/skills/**",)), modes


def test_the_authored_route_is_nested_inside_skills(call: dict[str, Any]) -> None:
    """`/skills/authored/` must be its own route, and `/skills/` must survive it.

    `CompositeBackend` sorts longest-prefix-first, so the nested route wins for
    its own prefix while the bundled skills keep resolving to the read-only
    filesystem backend. Losing the second route would make every bundled skill
    silently disappear.
    """
    backend = call["backend"](None)
    routes = set(backend.routes)
    assert "/skills/authored/" in routes
    assert "/skills/" in routes
    assert "/memories/" in routes


def test_authored_skills_are_project_scoped(call: dict[str, Any]) -> None:
    """The namespace is resolved per call, not frozen at construction.

    A constant tuple would put every project's authored skills in one bucket
    that every project then reads — and a skill is an instruction the model
    follows, so that is a cross-tenant prompt injection with a durable store
    behind it. It also raises at the first write: `StoreBackend._get_namespace`
    CALLS this, so a tuple is a `TypeError`, not a quiet mistake.
    """
    backend = call["backend"](None)
    # `_namespace` is private, and reading it is the point: this asserts on the
    # object `StoreBackend._get_namespace` will actually call, not on what was
    # handed to the constructor. A deepagents change that stopped storing it
    # here should break this test rather than pass it silently.
    namespace = backend.routes["/skills/authored/"]._namespace
    assert callable(namespace), "a tuple here raises TypeError at the first write"
    # No project resolvable outside a graph run: the fallback must not collide
    # with a real project id.
    assert namespace(None) == ("shared", "skills")


def test_the_authored_write_is_observed(call: dict[str, Any]) -> None:
    """Without the middleware there is no ledger row and no metadata refresh."""
    names = [type(m).__name__ for m in call["middleware"]]
    assert AuthoredSkillsMiddleware.__name__ in names
