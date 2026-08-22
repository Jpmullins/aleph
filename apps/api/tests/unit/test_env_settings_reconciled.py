"""WP-5 A7: `deploy/compose/.env.example` ↔ settings reconciliation.

Guards two directions so the example env and the pydantic settings models
cannot silently drift apart:

  (1) every REQUIRED settings field (no default) on either `Settings`
      (aleph-api) or `WorkerSettings` (aleph-workers) is satisfiable — its
      UPPER-CASE env name appears in `.env.example`, unless it is documented
      in `REQUIRED_IGNORE` as container/compose-injected.
  (2) every `ALEPH_*` key present in `.env.example` maps to a real field on
      one of the two settings models, unless documented in `ALEPH_KEY_IGNORE`
      (compose/frontend-only keys with no backend field).

No DB / lifespan is touched — this only introspects `model_fields` and parses
a text file, so it is a pure unit test.
"""

from __future__ import annotations

from pathlib import Path

from aleph_api.settings import Settings
from aleph_workers.settings import WorkerSettings

# --- ignore-lists (each entry justified) -----------------------------------

# Required fields that are legitimately NOT in `.env.example` because compose
# (or another orchestrator) injects them directly. Empty today: every required
# field on both models has a matching key in `.env.example`. Kept as the
# documented seam for future container-injected required settings.
REQUIRED_IGNORE: set[str] = set()

# `ALEPH_*` keys in `.env.example` that intentionally have no backend settings
# field.
ALEPH_KEY_IGNORE: dict[str, str] = {
    # Frontend-only: the web OIDC client id. Consumed by the SPA (mirrored via
    # VITE_AUTH_MODE); no aleph-api / aleph-workers field reads it.
    "ALEPH_AUTH_CLIENT_ID": "frontend-only web OIDC client id, no backend field",
    # Read directly by `create_app` via os.environ to build the CORS allow-list,
    # deliberately not a Settings field: it is a property of where the browser
    # loaded the page from, not of the API, and compose derives it from WEB_PORT.
    "ALEPH_CORS_ORIGINS": "read by create_app for the CORS allow-list, no settings field",
    # Read by `aleph_models.hints.load_hints` from `os.environ`, deliberately not
    # a Settings field. `aleph-models` is used by BOTH aleph-api and
    # aleph-workers and has no Settings of its own; promoting the path would
    # mean threading it Settings → GatewayCatalog → discover_models →
    # apply_hints → load_hints in two processes, to configure the location of a
    # file. It is a deployment-level path like a mount point, not a behaviour of
    # the API. Documented in .env.example so an operator can find it.
    "ALEPH_MODEL_HINTS_PATH": (
        "operator-supplied rates file path, read from os.environ by aleph-models"
    ),
    "ALEPH_HARNESS_PROFILES_PATH": (
        "operator-supplied per-model harness profile file, read from os.environ by aleph-api"
    ),
}


def _repo_root() -> Path:
    """Walk up from this file until we find deploy/compose/.env.example."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "deploy" / "compose" / ".env.example").is_file():
            return parent
    raise AssertionError("could not locate repo root containing deploy/compose/.env.example")


def _parse_env_example() -> dict[str, str]:
    """Parse KEY=VALUE lines, ignoring comments (# ...) and blank lines."""
    path = _repo_root() / "deploy" / "compose" / ".env.example"
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _required_env_names(model: type) -> set[str]:
    """UPPER-CASE env names for every required (no-default) field on a model.

    env_prefix is "" and case_sensitive is False for both models, so the env
    var name is simply the field name upper-cased.
    """
    return {name.upper() for name, field in model.model_fields.items() if field.is_required()}


def _all_field_env_names(model: type) -> set[str]:
    return {name.upper() for name in model.model_fields}


def test_required_settings_have_env_keys() -> None:
    env = _parse_env_example()
    env_keys = set(env)

    missing: list[str] = []
    for model in (Settings, WorkerSettings):
        for name in _required_env_names(model):
            if name in REQUIRED_IGNORE:
                continue
            if name not in env_keys:
                missing.append(f"{model.__name__}.{name}")

    assert not missing, (
        "required settings fields with no key in deploy/compose/.env.example "
        f"(add the key or document it in REQUIRED_IGNORE): {sorted(missing)}"
    )


def test_env_aleph_keys_map_to_settings_fields() -> None:
    env = _parse_env_example()
    known_fields = _all_field_env_names(Settings) | _all_field_env_names(WorkerSettings)

    orphans: list[str] = []
    for key in env:
        if not key.startswith("ALEPH_"):
            continue
        if key in ALEPH_KEY_IGNORE:
            continue
        if key not in known_fields:
            orphans.append(key)

    assert not orphans, (
        "ALEPH_* keys in deploy/compose/.env.example with no matching settings "
        f"field (add the field or document it in ALEPH_KEY_IGNORE): {sorted(orphans)}"
    )
