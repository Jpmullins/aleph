"""Pydantic-settings for the FastAPI app.

All settings read from env. `.env` is loaded by `pydantic-settings`
when present locally; in compose / k8s the env is set directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment
    aleph_env: Literal["local", "dev", "staging", "prod"] = "local"
    aleph_default_model_profile: Literal["aleph-dev", "aleph-production"] = "aleph-dev"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    service_name: str = "aleph-api"
    service_version: str = "0.1.0"

    # Database
    database_url: str

    # Redis
    redis_url: str

    # MinIO. Inc 1 routes use it for source asset storage.
    minio_endpoint: str | None = None
    aleph_s3_bucket: str | None = None
    minio_root_user: str | None = None
    minio_root_password: str | None = None

    # Langfuse
    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: str

    # OTEL
    otel_exporter_otlp_endpoint: str

    # LiteLLM gateway (single LLM transport)
    litellm_base_url: str
    insights_litellm_api_key: str

    # Auth
    aleph_auth_issuer: str
    aleph_auth_audience: str = "aleph"
    aleph_auth_jwks_url: str

    # Agent-token signing
    aleph_agent_token_secret: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
