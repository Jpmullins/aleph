"""A different prompt and tool set for a different model. WS-MEP-7.

Aleph sends the same 6.5k-character instruction block and the same orchestrator
tool set to every model, whether that is a frontier model or a 7B model someone
is running on a laptop. Once WS-MEP-4 lets a project point itself at Ollama or
vLLM, "one prompt for every model" stops being a tuning nicety and becomes the
thing that makes small models unusable — they are handed thirty tools and a
plan-and-delegate prompt written for a model that can hold thirty tools in mind.

deepagents 0.6.12 already has the mechanism: `register_harness_profile(key,
HarnessProfileConfig(...))`, consulted by `create_deep_agent` once the chat
model exists. This module is the part deepagents cannot supply — where the
profiles come from, what shape a key is allowed to be, and what happens when the
file is wrong.

**The key shape is the whole design, and it is the one thing that fails
silently.** `register_harness_profile` accepts a bare provider key (`"openai"`),
which applies to EVERY model reached through that provider. Aleph reaches every
model — Anthropic, Bedrock, a local vLLM — through one OpenAI-compatible
gateway, so every one of them resolves to provider `openai`. A profile
registered under `"openai"` to make a 7B model workable would therefore also
rewrite the prompt and remove tools for the frontier model on the same gateway,
and nothing would report it: the agent would simply get quieter and worse.
`parse_profiles` refuses a key with no `:` for that reason, and says so.

**A wrong file is louder than no file.** A typo in a field name is the failure
this class of feature dies of: `system_prompt_sufix` parses as YAML, registers
nothing, and leaves an operator convinced they tuned a model. Unknown fields,
unparseable YAML and a configured path that does not exist are all errors that
name the file. A path that was never configured is not an error — that is the
normal deployment.

**Registration must precede construction**, because `create_deep_agent` reads
the registry once, while it assembles the prompt and the middleware stack. A
profile registered afterwards affects the next graph and not this one, and the
symptom is a profile that "works after a restart" — so
`copilot_agent.build_assistant_deep_agent` calls
:func:`ensure_harness_profiles_registered` before it builds anything, and
`tests/integration/test_harness_profiles_reach_the_model.py` asserts the suffix on the wire
rather than in the registry.

Configured with `ALEPH_HARNESS_PROFILES_PATH`, read from `os.environ` rather
than promoted to a `Settings` field for the reason `ALEPH_MODEL_HINTS_PATH` is:
it is the location of an operator-supplied file, a deployment-level path like a
mount point, not a behaviour of the API.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import TYPE_CHECKING, Any

import structlog
from deepagents import HarnessProfileConfig, register_harness_profile

from aleph_core.errors import ValidationFailed

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

__all__ = [
    "HARNESS_PROFILES_ENV",
    "ensure_harness_profiles_registered",
    "load_harness_profiles",
    "parse_profiles",
    "profile_key",
    "register_profiles",
    "reset_harness_profile_registration",
]

_log = structlog.get_logger(__name__)

#: Where the operator's profile file lives. Unset is the normal deployment.
HARNESS_PROFILES_ENV = "ALEPH_HARNESS_PROFILES_PATH"

#: The declarative fields `HarnessProfileConfig` accepts. Listed here so an
#: unknown key is rejected with the file, the profile key and the valid names,
#: rather than as a bare `TypeError` from a dataclass constructor.
_CONFIG_FIELDS = frozenset(
    {
        "base_system_prompt",
        "system_prompt_suffix",
        "tool_description_overrides",
        "excluded_tools",
        "excluded_middleware",
        "general_purpose_subagent",
    }
)

#: Set once `load_harness_profiles` has run in this process. deepagents merges
#: repeated registrations under one key rather than replacing them, so calling
#: this per graph build would union the same exclusions on every turn — cheap,
#: but it makes "what is registered" depend on how many turns have happened.
#: Lower-case because it is mutable state, not a constant: pyright strict
#: refuses to let an upper-case name be reassigned, and it is right to.
_registered_keys: list[str] | None = None


def profile_key(*, provider: str, model: str) -> str:
    """The only key shape Aleph registers under: `provider:model`.

    A helper rather than an f-string at each call site so the refusal below and
    the thing that builds a key cannot disagree about what a key is.
    """
    cleaned_provider = provider.strip()
    cleaned_model = model.strip()
    if not cleaned_provider or not cleaned_model:
        msg = f"a harness profile key needs both halves; got {provider!r}:{model!r}"
        raise ValidationFailed(msg)
    return f"{cleaned_provider}:{cleaned_model}"


def _reject(source: str, detail: str) -> ValidationFailed:
    return ValidationFailed(f"harness profile file {source}: {detail}")


def _load_document(text: str, *, source: str) -> Any:
    """YAML or JSON, chosen by the file's own extension.

    JSON is parsed by the stdlib so a deployment that has not installed PyYAML
    can still carry profiles; YAML is what the plan asks for and what an
    operator will write a multi-line prompt suffix in.
    """
    if source.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise _reject(source, f"is not valid JSON: {exc}") from exc
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - PyYAML ships today
        raise _reject(source, "needs PyYAML to be installed, or a .json extension") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _reject(source, f"is not valid YAML: {exc}") from exc


def parse_profiles(text: str, *, source: str) -> dict[str, HarnessProfileConfig]:
    """Parse an operator's profile file. Every failure names the file.

    `source` is the filename and is not decorative: a boot that fails on
    "invalid YAML" with no path is unactionable when a deployment mounts its
    configuration from three places.
    """
    document = _load_document(text, source=source)
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise _reject(source, f"must be a mapping at the top level, got {type(document).__name__}")
    raw_profiles = document.get("profiles", document)
    if not isinstance(raw_profiles, dict):
        raise _reject(source, "the `profiles` key must be a mapping of key -> profile")

    parsed: dict[str, HarnessProfileConfig] = {}
    for key, body in raw_profiles.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str):
            raise _reject(source, f"profile keys must be strings, got {key!r}")
        if ":" not in key:
            raise _reject(
                source,
                f"profile key {key!r} names a PROVIDER, not a model. Aleph reaches every "
                f"model through one OpenAI-compatible gateway, so a provider-wide profile "
                f"applies to all of them at once — including the frontier model on the same "
                f"endpoint. Use '{key}:<model-id>'",
            )
        provider, _, model = key.partition(":")
        if not provider.strip() or not model.strip():
            raise _reject(source, f"profile key {key!r} has an empty half")
        if not isinstance(body, dict):
            raise _reject(source, f"profile {key!r} must be a mapping, got {type(body).__name__}")
        parsed[key] = _to_config(key, body, source=source)
    return parsed


def _to_config(key: str, body: Mapping[str, Any], *, source: str) -> HarnessProfileConfig:
    unknown = sorted(set(body) - _CONFIG_FIELDS)
    if unknown:
        raise _reject(
            source,
            f"profile {key!r} sets unknown field(s) {unknown}. A misspelled field parses "
            f"cleanly and configures nothing, which is why this is an error rather than a "
            f"warning. Valid fields: {sorted(_CONFIG_FIELDS)}",
        )
    fields: dict[str, Any] = dict(body)
    # `HarnessProfileConfig` wants sets for these two; YAML gives lists.
    for name in ("excluded_tools", "excluded_middleware"):
        value = fields.get(name)
        if value is not None:
            if not isinstance(value, list | set | tuple):
                raise _reject(source, f"profile {key!r}: `{name}` must be a list of names")
            fields[name] = frozenset(str(v) for v in value)  # pyright: ignore[reportUnknownArgumentType]
    try:
        return HarnessProfileConfig(**fields)
    except (TypeError, ValueError) as exc:  # pragma: no cover - guarded by `unknown` above
        raise _reject(source, f"profile {key!r} is not a valid harness profile: {exc}") from exc


def register_profiles(profiles: Mapping[str, HarnessProfileConfig]) -> list[str]:
    """Register each profile with deepagents, refusing a bare provider key.

    The refusal is repeated here rather than left to `parse_profiles` because
    this is the function a future caller (an autoconfigure pass deriving
    profiles from gateway metadata) will use, and the blast radius of a bare
    key does not depend on where the key came from.
    """
    registered: list[str] = []
    for key, profile in sorted(profiles.items()):
        if ":" not in key:
            msg = (
                f"refusing to register harness profile under the bare provider key {key!r}: "
                f"every Aleph model resolves to one gateway provider, so this would apply "
                f"to every model on the endpoint"
            )
            raise ValidationFailed(msg)
        register_harness_profile(key, profile)
        registered.append(key)
    return registered


def load_harness_profiles(path: str | pathlib.Path | None = None) -> list[str]:
    """Load and register the operator's profiles. Returns the keys registered.

    `path` defaults to `ALEPH_HARNESS_PROFILES_PATH`. Unset means no profiles,
    which is the normal deployment and not an error. A path that IS set and
    does not exist IS an error: an operator who configured a file and typed the
    path wrong must find out at boot, not from an assistant that behaves as if
    they had configured nothing.
    """
    target = pathlib.Path(path) if path is not None else None
    if target is None:
        configured = os.environ.get(HARNESS_PROFILES_ENV, "").strip()
        if not configured:
            return []
        target = pathlib.Path(configured)
    if not target.is_file():
        msg = (
            f"harness profile file {target} does not exist. {HARNESS_PROFILES_ENV} names it; "
            f"unset that variable if this deployment has no per-model profiles."
        )
        raise ValidationFailed(msg)
    profiles = parse_profiles(target.read_text(encoding="utf-8"), source=str(target))
    keys = register_profiles(profiles)
    _log.info("harness_profiles.loaded", path=str(target), keys=keys)
    return keys


def ensure_harness_profiles_registered() -> list[str]:
    """Load the operator's profiles once per process, before any graph is built.

    Idempotent because `create_deep_agent` runs per resolution and deepagents
    MERGES repeated registrations under one key rather than replacing them —
    so a per-build call would union the same exclusion set on every turn and
    make "what is registered" a function of how many turns have happened.
    """
    global _registered_keys
    if _registered_keys is None:
        _registered_keys = load_harness_profiles()
    return list(_registered_keys)


def reset_harness_profile_registration() -> None:
    """Forget that loading happened. Test hygiene, and the boot-time inverse.

    Does NOT unregister from deepagents: its registry is process-global and has
    no removal API. A test that needs a clean registry must use keys of its own.
    """
    global _registered_keys
    _registered_keys = None
