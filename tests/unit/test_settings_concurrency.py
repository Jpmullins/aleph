"""Concurrency-bound settings: arq worker slots + in-flight AIQ job cap.

Both exist so a local 16 GiB host can run the full stack with bootstrap-on-
create enabled: ARQ_MAX_JOBS bounds worker-side parallelism, and
AIQ_MAX_CONCURRENT_JOBS bounds how many research jobs run inside aiq-server
at once (the 2026-06-11 host crash was an unthrottled 21-job stampede).
"""

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


def test_worker_settings_concurrency_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ALEPH_API_INTERNAL_URL", "http://aleph-api:8000")
    from aleph_workers.settings import WorkerSettings

    s = WorkerSettings()  # type: ignore[call-arg]
    assert s.arq_max_jobs == 10
    assert s.aiq_max_concurrent_jobs == 3


def test_worker_settings_concurrency_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ALEPH_API_INTERNAL_URL", "http://aleph-api:8000")
    monkeypatch.setenv("ARQ_MAX_JOBS", "4")
    monkeypatch.setenv("AIQ_MAX_CONCURRENT_JOBS", "2")
    from aleph_workers.settings import WorkerSettings

    s = WorkerSettings()  # type: ignore[call-arg]
    assert s.arq_max_jobs == 4
    assert s.aiq_max_concurrent_jobs == 2


def test_api_settings_aiq_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    from aleph_api.settings import Settings

    assert Settings().aiq_max_concurrent_jobs == 3  # type: ignore[call-arg]
    monkeypatch.setenv("AIQ_MAX_CONCURRENT_JOBS", "5")
    assert Settings().aiq_max_concurrent_jobs == 5  # type: ignore[call-arg]
