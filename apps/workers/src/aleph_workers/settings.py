"""Worker settings — env-driven, same shape as the API where overlapping."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    aleph_env: str = "local"
    service_name: str = "aleph-workers"
    service_version: str = "0.1.0"
    aleph_default_model_profile: str = "aleph-dev"

    database_url: str
    redis_url: str

    # MinIO / S3 for the asset store used by normalize/chunk_embed jobs.
    minio_endpoint: str = "http://minio:9000"
    minio_root_user: str = "aleph"
    minio_root_password: str = "changeme-local"
    aleph_s3_bucket: str = "aleph-local"

    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: str
    otel_exporter_otlp_endpoint: str

    litellm_base_url: str
    insights_litellm_api_key: str

    aleph_api_internal_url: str
    aleph_agent_token_secret: str

    aiq_base_url: str = "http://aiq-server:8000"

    # Concurrency bounds. arq_max_jobs caps concurrent jobs per worker
    # process; aiq_max_concurrent_jobs caps research jobs in flight inside
    # aiq-server across ALL submitters (shared Redis gate — see
    # aleph_aiq.throttle). Lower both on memory-constrained hosts.
    arq_max_jobs: int = 10
    aiq_max_concurrent_jobs: int = 3

    # Bootstrap-on-create (mirrors aleph_api.settings.Settings). The
    # bootstrap_project_job reads these to bound the fan-out.
    bootstrap_auto_enabled: bool = True
    bootstrap_max_topics: int = 3
    bootstrap_depth: str = "shallow"


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()  # type: ignore[call-arg]
