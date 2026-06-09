"""Bootstrap-on-create settings load on both the API and worker Settings."""

from __future__ import annotations

import pytest


def _base_env() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://x:y@localhost/z",
        "REDIS_URL": "redis://localhost:6379/0",
        "LANGFUSE_HOST": "http://localhost:3000",
        "LANGFUSE_PUBLIC_KEY": "pk",
        "LANGFUSE_SECRET_KEY": "sk",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        "LITELLM_BASE_URL": "http://localhost:1",
        "INSIGHTS_LITELLM_API_KEY": "k",
        "ALEPH_AGENT_TOKEN_SECRET": "s",
    }


def test_api_settings_bootstrap_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    from aleph_api.settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.bootstrap_auto_enabled is True
    assert s.bootstrap_max_topics == 3
    assert s.bootstrap_depth == "shallow"


def test_api_settings_bootstrap_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BOOTSTRAP_AUTO_ENABLED", "false")
    monkeypatch.setenv("BOOTSTRAP_MAX_TOPICS", "5")
    from aleph_api.settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.bootstrap_auto_enabled is False
    assert s.bootstrap_max_topics == 5


def test_worker_settings_bootstrap_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ALEPH_API_INTERNAL_URL", "http://aleph-api:8000")
    from aleph_workers.settings import WorkerSettings

    s = WorkerSettings()  # type: ignore[call-arg]
    assert s.bootstrap_auto_enabled is True
    assert s.bootstrap_max_topics == 3
    assert s.bootstrap_depth == "shallow"
