"""AIQ callback endpoints — only callable by the aiq-server via service token.

These live under /internal/v1/aiq/ and are NOT exposed publicly in
production (the compose network restricts /internal/* to internal
services). The service token's project_id + agent_run_id is the
authorization context; the user JWT does NOT apply here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_aiq.auth_bridge import verify_service_token
from aleph_aiq.tokenomics_adapter import PhaseStat, record_aiq_phase_stats
from aleph_connectors.credentials import (
    ConnectorCredentialService,
    LibsodiumSealedBoxCipher,
)
from aleph_core.errors import NotFound, PermissionDenied
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentEvent
from aleph_db.repos.ledger import LedgerWriter
from aleph_rks.models import Connector, ConnectorBinding
from aleph_rks.source_service import register_uploaded_source

router = APIRouter(prefix="/internal/v1/aiq", tags=["aiq-internal"])


def _verify_token(request: Request, token: str | None):
    if not token:
        msg = "X-Aleph-Service-Token required"
        raise PermissionDenied(msg)
    secret = request.app.state.settings.aleph_agent_token_secret
    return verify_service_token(token, secret=secret)


def _master_secret(request: Request) -> bytes:
    s = request.app.state.settings.aleph_agent_token_secret.encode("utf-8")
    return s if len(s) >= 32 else s.ljust(32, b"0")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class CredentialIn(BaseModel):
    pass  # the kind is in the path; no body required


class CredentialOut(BaseModel):
    plaintext: str


@router.post("/credentials/{connector_kind}", response_model=CredentialOut)
async def get_credential(
    request: Request,
    connector_kind: str,
    x_aleph_service_token: Annotated[str | None, Header()] = None,
) -> CredentialOut:
    claims = _verify_token(request, x_aleph_service_token)
    maker = request.app.state.session_maker
    async with maker() as session:
        connector = (
            await session.execute(select(Connector).where(Connector.kind == connector_kind))
        ).scalar_one_or_none()
        if connector is None:
            msg = f"unknown connector: {connector_kind}"
            raise NotFound(msg)
        # Project must allowlist this connector.
        binding = (
            await session.execute(
                select(ConnectorBinding).where(
                    ConnectorBinding.project_id == claims.project_id,
                    ConnectorBinding.connector_id == connector.id,
                    ConnectorBinding.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        # If no per-project binding, fall back to enabled_by_default.
        if binding is None and not connector.enabled_by_default:
            msg = f"connector {connector_kind} not enabled for project"
            raise PermissionDenied(msg)

        import os

        cipher = LibsodiumSealedBoxCipher(master_secret=_master_secret(request))
        # Container-env API keys are a DEV-ONLY convenience and must never be the
        # credential source in a real deployment (rule: credentials come from
        # ConnectorCredential, never container env). Gate the fallback on local
        # auth mode; under oidc a missing per-project credential raises.
        if request.app.state.settings.aleph_auth_mode == "local":
            defaults = {
                "tavily": os.environ.get("TAVILY_API_KEY", "") or "",
                "exa": os.environ.get("EXA_API_KEY", "") or "",
                "serper": os.environ.get("SERPER_API_KEY", "") or "",
                "semantic_scholar": os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "") or "",
                "huggingface_hub": os.environ.get("HUGGINGFACE_API_KEY", "") or "",
                "lens": os.environ.get("LENS_API_KEY", "") or "",
            }
        else:
            defaults = {}
        svc = ConnectorCredentialService(session, cipher=cipher, dev_default_for=defaults)
        plaintext = await svc.decrypt_for_callback(
            project_id=claims.project_id,
            connector_id=connector.id,
            connector_kind=connector_kind,
        )
    return CredentialOut(plaintext=plaintext)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class AIQSourceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connector_kind: str
    external_id: str
    title: str
    url: str | None = None
    metadata: dict = Field(default_factory=dict)
    raw_b64: str
    """base64-encoded raw bytes."""
    mime_type: str = "application/octet-stream"
    filename: str | None = None


class AIQSourceOut(BaseModel):
    source_id: str
    source_short_id: str


@router.post("/sources", response_model=AIQSourceOut)
async def persist_source(
    request: Request,
    body: Annotated[AIQSourceIn, Body()],
    x_aleph_service_token: Annotated[str | None, Header()] = None,
) -> AIQSourceOut:
    claims = _verify_token(request, x_aleph_service_token)
    import base64

    raw = base64.b64decode(body.raw_b64.encode("ascii"))
    filename = body.filename or f"{body.external_id[:120]}.bin"

    maker = request.app.state.session_maker
    asset_store = request.app.state.asset_store
    async with maker() as session:
        ledger = LedgerWriter(session)
        from aleph_security.principal import Principal

        aiq_principal = Principal(
            user_id=claims.principal_user_id,
            subject="aiq",
            email="",
            actor_kind="aiq_agent",
            agent_run_id=claims.agent_run_id,
            correlation_id=None,
        )
        created = await register_uploaded_source(
            session,
            ledger=ledger,
            principal=aiq_principal,
            asset_store=asset_store,
            project_id=claims.project_id,
            title=body.title,
            data=raw,
            filename=filename,
            mime_type=body.mime_type,
        )
        # Carry AIQ-supplied metadata onto the source row.
        if body.metadata:
            created.source.source_metadata_jsonb = {
                **created.source.source_metadata_jsonb,
                "aiq_metadata": body.metadata,
            }
        await session.commit()
        return AIQSourceOut(
            source_id=str(created.source.id),
            source_short_id=created.source.short_id,
        )


# ---------------------------------------------------------------------------
# Model calls (tokenomics)
# ---------------------------------------------------------------------------


class AIQCallIn(BaseModel):
    phase: str
    model: str
    capability: str = "synthesis"
    input_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: str = "0"
    cache_savings_usd: str = "0"
    latency_ms: int = 0
    timestamp: datetime
    trace_id: str | None = None


class AIQModelCallsIn(BaseModel):
    calls: list[AIQCallIn]


@router.post("/model-calls")
async def record_model_calls(
    request: Request,
    body: Annotated[AIQModelCallsIn, Body()],
    x_aleph_service_token: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    claims = _verify_token(request, x_aleph_service_token)
    stats = [
        PhaseStat(
            phase=c.phase,
            model=c.model,
            capability=c.capability,
            input_tokens=c.input_tokens,
            cached_tokens=c.cached_tokens,
            completion_tokens=c.completion_tokens,
            cost_usd=Decimal(c.cost_usd),
            cache_savings_usd=Decimal(c.cache_savings_usd),
            latency_ms=c.latency_ms,
            timestamp=c.timestamp,
            trace_id=c.trace_id,
        )
        for c in body.calls
    ]
    maker = request.app.state.session_maker
    async with maker() as session:
        n = await record_aiq_phase_stats(
            session,
            project_id=claims.project_id,
            agent_run_id=claims.agent_run_id,
            stats=stats,
        )
        await session.commit()
    return {"recorded": n}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class AIQEventIn(BaseModel):
    event_kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AIQEventsIn(BaseModel):
    events: list[AIQEventIn]


@router.post("/events")
async def record_events(
    request: Request,
    body: Annotated[AIQEventsIn, Body()],
    x_aleph_service_token: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    claims = _verify_token(request, x_aleph_service_token)
    maker = request.app.state.session_maker
    async with maker() as session:
        for e in body.events:
            session.add(
                AgentEvent(
                    id=uuid7(),
                    agent_run_id=claims.agent_run_id,
                    event_kind=e.event_kind,
                    payload_jsonb=e.payload,
                    timestamp=utcnow(),
                )
            )
        await session.commit()
    return {"recorded": len(body.events)}
