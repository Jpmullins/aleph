"""The profile file, and the two ways it destroys an agent silently. WS-MEP-7.

Both failures this file guards produce a working assistant that is quietly
wrong, which is why they are errors rather than warnings:

* **A bare provider key.** `register_harness_profile("openai", ...)` applies to
  every model reached through that provider — and Aleph reaches ALL of its
  models through one OpenAI-compatible gateway, so "openai" means "everything on
  this deployment". A profile written to make a 7B model usable would rewrite
  the frontier model's prompt and take away its tools, and the only symptom is
  an agent that got worse.
* **A misspelled field.** `system_prompt_sufix` is valid YAML. It registers a
  profile that configures nothing, and leaves an operator certain they tuned a
  model.

The behavioural half — a registered profile actually reaching the wire, before
the graph is built — is `tests/integration/test_harness_profiles_reach_the_model.py`,
which reads the tool list and the system prompt out of a request the gateway
recorded. Nothing here asserts that a registry entry has an effect; a registry
that is read by nobody is the defect class this repository is named for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aleph_api.harness_profiles import (
    HARNESS_PROFILES_ENV,
    ensure_harness_profiles_registered,
    load_harness_profiles,
    parse_profiles,
    profile_key,
    register_profiles,
    reset_harness_profile_registration,
)
from aleph_core.errors import ValidationFailed

GOOD = """
profiles:
  "openai:tiny-local-model":
    system_prompt_suffix: |
      Answer in one step. Do not delegate.
    excluded_tools:
      - start_background_task
      - author_plugin
"""


@pytest.fixture(autouse=True)
def _forget_registration() -> object:
    """`ensure_harness_profiles_registered` latches; a leaked latch decides the
    result of the next test."""
    reset_harness_profile_registration()
    yield
    reset_harness_profile_registration()


# ---------------------------------------------------------------------------
# c2 — no profile under a bare provider key
# ---------------------------------------------------------------------------


def test_a_provider_wide_key_is_refused_by_the_parser() -> None:
    text = 'profiles:\n  "openai":\n    system_prompt_suffix: "hi"\n'
    with pytest.raises(ValidationFailed) as raised:
        parse_profiles(text, source="profiles.yaml")
    message = str(raised.value)
    assert "profiles.yaml" in message, "the error must name the file"
    assert "openai:<model-id>" in message, "the error must show the shape that works"


def test_a_provider_wide_key_is_refused_by_the_registrar_too() -> None:
    """Not only at parse time.

    `register_profiles` is the function a future autoconfigure pass will call
    with keys it derived from gateway metadata rather than read from a file,
    and the blast radius of a bare key does not depend on where it came from.
    """
    from deepagents import HarnessProfileConfig

    with pytest.raises(ValidationFailed) as raised:
        register_profiles({"openai": HarnessProfileConfig(system_prompt_suffix="hi")})
    assert "every model on the endpoint" in str(raised.value)


def test_a_key_with_an_empty_half_is_refused() -> None:
    for key in ("openai:", ":tiny-model", ": "):
        with pytest.raises(ValidationFailed):
            parse_profiles(
                f'profiles:\n  "{key}":\n    system_prompt_suffix: "x"\n', source="p.yaml"
            )


def test_profile_key_builds_the_only_shape_that_is_registered() -> None:
    assert profile_key(provider="openai", model="tiny-local-model") == "openai:tiny-local-model"
    with pytest.raises(ValidationFailed):
        profile_key(provider="openai", model="  ")


def test_a_model_key_is_accepted_and_parsed() -> None:
    """The positive half: refusing everything would satisfy every test above."""
    parsed = parse_profiles(GOOD, source="profiles.yaml")
    assert set(parsed) == {"openai:tiny-local-model"}
    profile = parsed["openai:tiny-local-model"]
    assert "Do not delegate" in (profile.system_prompt_suffix or "")
    assert profile.excluded_tools == frozenset({"start_background_task", "author_plugin"})


# ---------------------------------------------------------------------------
# c5 — a malformed profile file fails loudly, naming the file
# ---------------------------------------------------------------------------


def test_unparseable_yaml_names_the_file() -> None:
    with pytest.raises(ValidationFailed) as raised:
        parse_profiles("profiles:\n  - [unclosed\n", source="/etc/aleph/profiles.yaml")
    assert "/etc/aleph/profiles.yaml" in str(raised.value)


def test_an_unknown_field_is_an_error_not_a_shrug() -> None:
    """The typo that would otherwise configure nothing and report success."""
    text = 'profiles:\n  "openai:m":\n    system_prompt_sufix: "oops"\n'
    with pytest.raises(ValidationFailed) as raised:
        parse_profiles(text, source="profiles.yaml")
    message = str(raised.value)
    assert "system_prompt_sufix" in message
    assert "system_prompt_suffix" in message, "the error must show the field that was meant"


def test_a_top_level_list_is_an_error() -> None:
    with pytest.raises(ValidationFailed) as raised:
        parse_profiles("- openai:m\n", source="profiles.yaml")
    assert "mapping at the top level" in str(raised.value)


def test_a_profile_body_that_is_not_a_mapping_is_an_error() -> None:
    with pytest.raises(ValidationFailed) as raised:
        parse_profiles('profiles:\n  "openai:m": "a string"\n', source="profiles.yaml")
    assert "must be a mapping" in str(raised.value)


def test_excluded_tools_given_as_a_string_is_an_error() -> None:
    """`excluded_tools: start_background_task` is the natural typo, and a
    permissive parser would exclude eight single-letter tool names from it."""
    with pytest.raises(ValidationFailed) as raised:
        parse_profiles(
            'profiles:\n  "openai:m":\n    excluded_tools: start_background_task\n',
            source="profiles.yaml",
        )
    assert "must be a list of names" in str(raised.value)


def test_an_empty_file_is_no_profiles_not_an_error() -> None:
    assert parse_profiles("", source="profiles.yaml") == {}
    assert parse_profiles("# only a comment\n", source="profiles.yaml") == {}


def test_json_is_accepted_by_extension() -> None:
    """So a deployment without PyYAML can still carry profiles."""
    text = json.dumps({"profiles": {"openai:m": {"system_prompt_suffix": "hi"}}})
    parsed = parse_profiles(text, source="profiles.json")
    assert parsed["openai:m"].system_prompt_suffix == "hi"


def test_broken_json_names_the_file_and_says_json() -> None:
    with pytest.raises(ValidationFailed) as raised:
        parse_profiles("{not json", source="/srv/profiles.json")
    assert "/srv/profiles.json" in str(raised.value)
    assert "JSON" in str(raised.value)


# ---------------------------------------------------------------------------
# Loading: unset is normal, a wrong path is not
# ---------------------------------------------------------------------------


def test_no_configured_path_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HARNESS_PROFILES_ENV, raising=False)
    assert load_harness_profiles() == []
    assert ensure_harness_profiles_registered() == []


def test_a_configured_path_that_does_not_exist_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An operator who typed the path wrong must find out here.

    Skipping a missing file would leave them with an assistant that behaves
    exactly as if they had configured nothing, which is indistinguishable from
    the profile not working.
    """
    missing = tmp_path / "not-there.yaml"
    monkeypatch.setenv(HARNESS_PROFILES_ENV, str(missing))
    with pytest.raises(ValidationFailed) as raised:
        load_harness_profiles()
    assert str(missing) in str(raised.value)
    assert HARNESS_PROFILES_ENV in str(raised.value)


def test_a_real_file_registers_its_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(GOOD, encoding="utf-8")
    monkeypatch.setenv(HARNESS_PROFILES_ENV, str(path))
    assert load_harness_profiles() == ["openai:tiny-local-model"]

    from deepagents.profiles.harness.harness_profiles import _get_harness_profile

    # c2's criterion, verbatim: the bare provider key resolves to nothing and
    # the discovered-model key resolves to a profile.
    assert _get_harness_profile("openai") is None
    assert _get_harness_profile("openai:tiny-local-model") is not None


def test_loading_happens_once_per_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """deepagents MERGES repeated registrations under one key rather than
    replacing them, so a per-turn load would union the same exclusion set on
    every turn and make the registry a function of uptime."""
    path = tmp_path / "profiles.yaml"
    path.write_text(GOOD, encoding="utf-8")
    monkeypatch.setenv(HARNESS_PROFILES_ENV, str(path))

    first = ensure_harness_profiles_registered()
    path.unlink()  # a second load would now raise "does not exist"
    second = ensure_harness_profiles_registered()
    assert first == second == ["openai:tiny-local-model"]
