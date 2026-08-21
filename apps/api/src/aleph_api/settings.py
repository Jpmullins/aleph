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

    # Asset storage (WP-1). `fs` (default) keeps bytes under aleph_asset_root
    # and serves them only through the authenticated streaming route; `s3`
    # targets any S3-compatible endpoint (opt-in `s3` compose profile locally).
    aleph_asset_backend: Literal["fs", "s3"] = "fs"
    aleph_asset_root: str = "data/assets"
    aleph_s3_endpoint: str | None = None
    aleph_s3_access_key: str | None = None
    aleph_s3_secret_key: str | None = None
    aleph_s3_bucket: str | None = None
    aleph_s3_secure: bool = False

    # Langfuse
    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: str

    # OTEL
    otel_exporter_otlp_endpoint: str

    # LiteLLM gateway (single LLM transport)
    litellm_base_url: str
    insights_litellm_api_key: str

    # Aleph runs single-user. The OIDC mode was removed — see
    # docs/decisions.md D6. Kept as a literal so an old .env that still
    # sets ALEPH_AUTH_MODE=local keeps working.
    aleph_auth_mode: Literal["local"] = "local"

    # Local-mode dev principal. Fixed identity that the auth middleware
    # JIT-provisions on first sight so the User row exists and gets a
    # `user.create` ledger event, same as a real OIDC first-login.
    local_dev_subject: str = "local-dev"
    local_dev_email: str = "dev@aleph.local"
    local_dev_display_name: str = "Local Dev"

    # Agent-token signing
    aleph_agent_token_secret: str

    # Self URL. Agent tools that re-enter the API over HTTP (ingest_source,
    # start_research) call this base so the agent never touches the DB or
    # asset store directly (architecture rule #3). Overridable via ALEPH_SELF_URL.
    aleph_self_url: str = "http://localhost:8000"

    # Scholar (WP-2). `mailto` is the polite-pool contact sent to Crossref /
    # OpenAlex on every request; the cap meters Consensus searches per project
    # per month (Redis counter — the Pro plan meters 250/month upstream).
    aleph_scholar_mailto: str = "dev@aleph.local"
    aleph_consensus_monthly_search_cap: int = 200

    # Bootstrap-on-create. When a project is created, a background
    # `bootstrap_project_job` scopes the title+description into seed topics,
    # seeds an overview wiki page, and fans out research per topic. Cost is
    # bounded solely by `bootstrap_max_topics`; there is no per-action gating.
    bootstrap_auto_enabled: bool = True
    bootstrap_max_topics: int = 3
    bootstrap_depth: Literal["shallow", "deep"] = "shallow"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
