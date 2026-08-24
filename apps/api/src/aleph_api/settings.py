"""Pydantic-settings for the FastAPI app.

All settings read from env. `.env` is loaded by `pydantic-settings`
when present locally; in compose / k8s the env is set directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aleph_connectors.keys import legacy_read_key, master_key_bytes


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
    # Sampling temperature for the agent path, or None to OMIT the parameter.
    #
    # Omitting is the default because sending it is a HARD FAILURE on models that
    # have deprecated it — `claude-opus-4-7` via Bedrock returns
    # 400 "`temperature` is deprecated for this model", which surfaced as
    # `AgentModelUnavailable` and killed every chat turn. Omitting it costs
    # nothing: the model uses its own default.
    #
    # Aleph ships no model list, so it cannot know which models accept it. The
    # asymmetry decides the default: sending it can break a model, omitting it
    # cannot. Set ALEPH_AGENT_TEMPERATURE to opt back in.
    aleph_agent_temperature: float | None = None

    aleph_auth_mode: Literal["local"] = "local"

    # Local-mode dev principal. Fixed identity that the auth middleware
    # JIT-provisions on first sight so the User row exists and gets a
    # `user.create` ledger event, same as a real OIDC first-login.
    local_dev_subject: str = "local-dev"
    local_dev_email: str = "dev@aleph.local"
    local_dev_display_name: str = "Local Dev"

    # Agent-token signing. This secret does ONE job: it signs the short-lived
    # HS256 tokens workers use to call back into the API. It used to do three,
    # and the third was encrypting every stored connector credential — so the
    # correct response to a leaked signing key (rotate it) destroyed every
    # third-party API key and OAuth grant in the deployment, with no warning and
    # no way back. See `aleph_connectors.keys`.
    aleph_agent_token_secret: str

    #: Largest upload the API will accept, in bytes. 64 MiB.
    #:
    #: The upload route read the whole body into memory with no bound at all, so
    #: one large POST could take the process out — and with a hard `mem_limit`
    #: on the container that means OOM-killed, not slow.
    aleph_max_upload_bytes: int = 64 * 1024 * 1024

    # Credential encryption. Separate from the signing secret above, and that
    # separation is the entire point of WS-P7. Required, and validated here so a
    # bad value fails at boot rather than at the first decrypt — which is a
    # background job whose only symptom is a connector quietly dropping out of
    # the research loop.
    aleph_credential_master_key: str

    # The key that opens credentials written BEFORE the split (`key_version`
    # `v1`). Leave unset and the agent-token secret is assumed, because that is
    # in fact what encrypted them. Set it explicitly only if the signing secret
    # has already been rotated, in which case the old value lives nowhere else.
    # Remove it once `python -m aleph_connectors.reencrypt --dry-run` reports
    # zero rows — see the rotation procedure in docs/operations.md.
    aleph_credential_legacy_key: str = ""

    # Self URL. Agent tools that re-enter the API over HTTP (ingest_source,
    # start_research) call this base so the agent never touches the DB or
    # asset store directly (architecture rule #3). Overridable via ALEPH_SELF_URL.
    aleph_self_url: str = "http://localhost:8000"

    # Scholar (WP-2). `mailto` is the polite-pool contact sent to Crossref /
    # OpenAlex on every request; the cap meters Consensus searches per project
    # per month (Redis counter — the Pro plan meters 250/month upstream).
    # Empty by default, deliberately. A filled-in placeholder reads as configured
    # and buys nothing: the polite pool keys on an address a human answers, and
    # `is_contactable("dev@aleph.local")` is already False, so the only thing the
    # placeholder ever did was make an unset value look set.
    aleph_scholar_mailto: str = ""
    aleph_consensus_monthly_search_cap: int = 200

    # The agent's own Postgres pool and its gateway budget.
    #
    # The pool held exactly ONE connection: `AsyncConnectionPool` defaults
    # `max_size` to `min_size`, so every saved checkpoint, every memory read and
    # every concurrent subagent queued behind the same connection and gave up
    # after 30 seconds. A six-way subagent fan-out is six things contending for
    # one connection, which is the shape of "the assistant is slow and then
    # fails" that has no error message.
    #
    # `aleph_agent_pool_max_size` is deliberately modest next to the SQLAlchemy
    # engine's 10+20: this is a SECOND pool in the same process, and raising it
    # moves pressure onto the compose Postgres rather than removing it. Check
    # `max_connections` there before raising it further.
    aleph_agent_pool_min_size: int = 1
    aleph_agent_pool_max_size: int = 8
    aleph_agent_pool_timeout_s: float = 30.0

    # 60 seconds is below the p99 of a tool-heavy turn against a shared gateway,
    # and two immediate retries is the worst possible response to being rate
    # limited. Both were literals in `_gateway_chat_model`; the retry now happens
    # in `AlephAgentMiddleware.awrap_model_call` with real backoff, and the SDK's
    # own retry is turned OFF so the two do not stack.
    aleph_agent_request_timeout_s: float = 180.0
    aleph_agent_max_retries: int = 3
    aleph_agent_retry_base_delay_s: float = 1.0
    aleph_agent_retry_max_delay_s: float = 30.0

    # Bootstrap-on-create. When a project is created, a background
    # `bootstrap_project_job` scopes the title+description into seed topics,
    # seeds an overview wiki page, and fans out research per topic. Cost is
    # bounded solely by `bootstrap_max_topics`; there is no per-action gating.
    bootstrap_auto_enabled: bool = True
    bootstrap_max_topics: int = 3
    bootstrap_depth: Literal["shallow", "deep"] = "shallow"

    @field_validator("aleph_credential_master_key")
    @classmethod
    def _check_master_key(cls, value: str) -> str:
        """Refuse a short or placeholder master key AT BOOT.

        `get_settings()` is called from the lifespan, so this fires when the
        process starts rather than at the first decrypt. The rejected values are
        the two that used to be silently tolerated: anything under 32 bytes (the
        old code right-padded it with ASCII zeros, so the cipher's own length
        guard could never fire) and the `.env.example` placeholder, which is
        exactly 32 characters and therefore passes a length check while being
        published in this repository.
        """
        master_key_bytes(value)
        return value

    @field_validator("aleph_agent_token_secret")
    @classmethod
    def _check_agent_token_secret(cls, value: str) -> str:
        """The signing secret gets the same guard, for the same two reasons.

        WS-P7 gave `ALEPH_CREDENTIAL_MASTER_KEY` a boot-time check and left this
        one declared as a bare `str`. Both are published in `.env.example` as
        `CHANGE-ME-run-openssl-rand-hex-32`, and this one signs the agent tokens
        that authorise a worker to re-enter the API — so a deployment that keeps
        the placeholder is one where anybody holding this repository can mint a
        token it will accept.

        The failure mode is worse than the master key's, not better: a weak
        encryption key is discovered when someone tries to read the data, while
        a weak signing key is discovered when someone uses it.
        """
        master_key_bytes(value, setting="ALEPH_AGENT_TOKEN_SECRET")
        return value

    @property
    def credential_legacy_key(self) -> str:
        """The secret that opens `v1` credential rows."""
        return legacy_read_key(self.aleph_credential_legacy_key, self.aleph_agent_token_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
