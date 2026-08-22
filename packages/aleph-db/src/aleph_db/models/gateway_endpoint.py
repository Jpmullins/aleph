"""Where a project's LLM gateway lives, and the key that opens it.

WS-MEP-4. Today the gateway is one process-wide pair of environment variables
(`LITELLM_BASE_URL`, `INSIGHTS_LITELLM_API_KEY`) read at boot and shared by
everything. That makes three ordinary things impossible: pointing two projects
at two gateways, changing an endpoint without a redeploy, and rotating a key
without one either. It also puts a live credential in container env, where it
is visible to every process, every crash dump and every `docker inspect`.

An endpoint is therefore a **row**, and its key is encrypted with the same
cipher `ConnectorCredential` uses — `cipher_scheme` and `key_version` are the
same columns with the same meaning, so `ALEPH_CREDENTIAL_MASTER_KEY` rotation
covers gateway keys with the machinery that already exists rather than a second
scheme that would need its own rotation story. `packages/aleph-connectors`
carries that cipher and the re-encryption pass; this table deliberately stores
nothing the cipher does not already know how to read.

**The key is never selected into a response.** `api_key_cipher` leaves the
server only through `aleph_models.endpoints.resolve_endpoint`, which decrypts
in process and hands back a value used to build an HTTP client. There is no
read path that returns it, the same rule `ConnectorCredential` follows.

STATE OF PLAY, read this before building on it: this is the data layer and its
resolver only. There is no HTTP route, no UI and no production caller — the
process still reads its endpoint from settings. The rest of MEP-4/5 wires it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class GatewayEndpoint(CommonColumns, Base):
    __tablename__ = "gateway_endpoints"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_gateway_endpoint_project_name"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)

    #: Operator-facing label, unique within the project. Not the URL: two rows
    #: may legitimately point at the same host with different keys (a
    #: restricted key and an admin one), and naming them by URL would make that
    #: unexpressible.
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Bare origin, no `/v1`. Both conventions exist in this codebase —
    #: `LiteLLMClient` appends `/v1/...` itself while `ChatOpenAI` needs the
    #: `/v1` already present — and storing the bare origin keeps the one
    #: normaliser (`_openai_base_url`) responsible for the difference. Storing
    #: whatever an operator pasted is how agent traffic went to
    #: `{base}/chat/completions` and 404'd.
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    #: The API key, encrypted. NULL is a real state, not a missing value: a
    #: gateway on a private network may need no key at all, and modelling that
    #: as an empty string would make "no key" and "key we failed to store"
    #: identical.
    api_key_cipher: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    #: Which cipher wrote `api_key_cipher`, and which master-key generation it
    #: used. Same values as `connector_credentials`, because it is the same
    #: cipher — `libsodium-sealed` / `v2`. NULL exactly when there is no key.
    cipher_scheme: Mapped[str | None] = mapped_column(String(32), nullable=True)
    key_version: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: The project's default endpoint. Enforced by the resolver, not by the
    #: schema: a partial unique index would make "demote A, promote B" fail
    #: halfway through unless it is done in one statement, and the resolver
    #: already has to pick deterministically when zero or two rows claim it.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    #: The last time anything actually talked to this endpoint, and what it
    #: said. A configured endpoint and a reachable one are different claims —
    #: the whole reason `probe_model` exists — so the row records the second
    #: rather than letting the first imply it. `last_probe_error` holds the
    #: gateway's own words: replacing them with "connection failed" is what
    #: sends an operator looking for a network problem when the answer was
    #: "invalid api key".
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_probe_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_probe_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    #: How many models it advertised at that probe. Zero from a reachable
    #: gateway is a real answer and a bad one — a virtual key with no models
    #: attached — and it is invisible if only `last_probe_ok` is recorded.
    last_probe_model_count: Mapped[int | None] = mapped_column(nullable=True)
