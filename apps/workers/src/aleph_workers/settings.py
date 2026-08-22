"""Worker settings — env-driven, same shape as the API where overlapping."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aleph_connectors.keys import legacy_read_key, master_key_bytes


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
    # Dedicated Redis for the isolated code_runner job bus (WP-4c). Defaults to
    # the platform Redis for non-compose/test contexts where no sandbox runs.
    code_runner_redis_url: str = "redis://redis:6379/0"

    # Asset storage (WP-1) — same selection as the API; workers read/write
    # the identical root (shared bind mount in compose).
    aleph_asset_backend: Literal["fs", "s3"] = "fs"
    aleph_asset_root: str = "data/assets"
    aleph_s3_endpoint: str | None = None
    aleph_s3_access_key: str | None = None
    aleph_s3_secret_key: str | None = None
    aleph_s3_bucket: str | None = None
    aleph_s3_secure: bool = False

    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: str
    otel_exporter_otlp_endpoint: str

    litellm_base_url: str
    insights_litellm_api_key: str

    aleph_api_internal_url: str
    #: Signs the short-lived callback tokens. ONE job — see
    #: `aleph_api.settings.Settings.aleph_agent_token_secret`.
    aleph_agent_token_secret: str

    #: Encrypts stored connector credentials. Workers decrypt them in-process
    #: when binding the research loop's tools, so the worker needs the same key
    #: the API has — and needs to fail at startup, not at the first research run,
    #: if it is missing or too short.
    aleph_credential_master_key: str
    #: Opens pre-split (`v1`) rows; defaults to the agent-token secret, which is
    #: what encrypted them. See docs/operations.md, "Rotating a secret".
    aleph_credential_legacy_key: str = ""

    # Concurrency bound: arq_max_jobs caps concurrent jobs per worker
    # process. Lower it on memory-constrained hosts.
    arq_max_jobs: int = 10

    # Bootstrap-on-create (mirrors aleph_api.settings.Settings). The
    # bootstrap_project_job reads these to bound the fan-out.
    bootstrap_auto_enabled: bool = True
    bootstrap_max_topics: int = 3
    bootstrap_depth: str = "shallow"

    # Scholar (WP-2, mirrors aleph_api.settings.Settings). The reviewer's
    # doi_verification pass (WP-3) runs in workers and needs the same polite
    # mailto + Consensus quota cap the API uses.
    aleph_scholar_mailto: str = "dev@aleph.local"
    aleph_consensus_monthly_search_cap: int = 200

    # Native research loop bounds (WP-3). The plateau cutoff (an iteration
    # ingesting 0 new sources stops the loop) applies regardless.
    research_max_iterations_deep: int = 3
    research_max_iterations_shallow: int = 1
    research_max_sources_per_iter: int = 6
    research_max_total_sources: int = 15

    # Credential env fallback gate (mirrors aleph_api.settings.Settings):
    # container-env API keys are honored only under local auth mode.
    aleph_auth_mode: str = "local"

    @field_validator("aleph_credential_master_key")
    @classmethod
    def _check_master_key(cls, value: str) -> str:
        """Same boot-time refusal as the API. A worker that starts with a bad
        credential key does not fail until a research run tries to decrypt, and
        that failure is swallowed by design — `resolve_bound_tools` skips a
        connector whose credential will not open rather than aborting the run.
        So without this the symptom is "the research loop found less than it
        used to", which nobody traces back to a secret."""
        master_key_bytes(value)
        return value

    @property
    def credential_legacy_key(self) -> str:
        return legacy_read_key(self.aleph_credential_legacy_key, self.aleph_agent_token_secret)


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()  # type: ignore[call-arg]
