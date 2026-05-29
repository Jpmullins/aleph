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


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()  # type: ignore[call-arg]
