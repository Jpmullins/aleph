"""Connector credentials API.

Owner-only. Plaintext is never returned or stored in any ledger
payload. Encryption uses libsodium sealed-box per project (dev) or
KMS-AES-GCM (prod) — see `aleph_connectors.credentials`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_connectors.credentials import (
    ConnectorCredential,
    ConnectorCredentialService,
    LibsodiumSealedBoxCipher,
)
from aleph_core.errors import NotFound
from aleph_rks.models import Connector
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

router = APIRouter(prefix="/v1/projects", tags=["connector-credentials"])


class CredentialPlaintextIn(BaseModel):
    plaintext: str = Field(min_length=1, max_length=8192)


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connector_kind: str
    has_project_specific: bool
    rotated_at: str | None = None


def _master_secret(request: Request) -> bytes:
    s = request.app.state.settings.aleph_agent_token_secret.encode("utf-8")
    # 32 bytes minimum; the token secret is generated with openssl rand -hex 32
    # (64 hex chars = 32 bytes), so even the byte-form satisfies the cipher.
    return s if len(s) >= 32 else s.ljust(32, b"0")


def _service(request: Request, session) -> ConnectorCredentialService:
    cipher = LibsodiumSealedBoxCipher(master_secret=_master_secret(request))
    # Pull dev-default fallbacks from env.
    import os

    defaults = {
        "tavily": os.environ.get("TAVILY_API_KEY", "") or "",
        "exa": os.environ.get("EXA_API_KEY", "") or "",
        "serper": os.environ.get("SERPER_API_KEY", "") or "",
        "semantic_scholar": os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "") or "",
        "huggingface_hub": os.environ.get("HUGGINGFACE_API_KEY", "") or "",
        "lens": os.environ.get("LENS_API_KEY", "") or "",
    }
    return ConnectorCredentialService(
        session, cipher=cipher, dev_default_for=defaults
    )


async def _connector_by_kind(session, kind: str) -> Connector:
    c = (
        await session.execute(select(Connector).where(Connector.kind == kind))
    ).scalar_one_or_none()
    if c is None:
        msg = f"unknown connector: {kind}"
        raise NotFound(msg)
    return c


@router.get(
    "/{project_id}/connector-credentials",
    response_model=list[CredentialOut],
)
async def list_creds(
    project_id: ProjectScopeDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> list[CredentialOut]:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    connectors = list(
        (await session.execute(select(Connector).order_by(Connector.kind))).scalars().all()
    )
    cred_rows = {
        c.connector_id: c
        for c in (
            await session.execute(
                select(ConnectorCredential).where(
                    ConnectorCredential.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    }
    out: list[CredentialOut] = []
    for c in connectors:
        has = c.id in cred_rows
        out.append(
            CredentialOut(
                connector_kind=c.kind,
                has_project_specific=has,
                rotated_at=cred_rows[c.id].rotated_at.isoformat() if has and cred_rows[c.id].rotated_at else None,
            )
        )
    return out


@router.put(
    "/{project_id}/connector-credentials/{connector_kind}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def upsert_cred(
    request: Request,
    project_id: ProjectScopeDep,
    connector_kind: str,
    body: Annotated[CredentialPlaintextIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> None:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    connector = await _connector_by_kind(session, connector_kind)
    svc = _service(request, session)
    await svc.upsert(
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        connector_id=connector.id,
        connector_kind=connector_kind,
        plaintext=body.plaintext,
    )


@router.delete(
    "/{project_id}/connector-credentials/{connector_kind}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_cred(
    request: Request,
    project_id: ProjectScopeDep,
    connector_kind: str,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> None:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    connector = await _connector_by_kind(session, connector_kind)
    svc = _service(request, session)
    removed = await svc.delete(
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        connector_id=connector.id,
        connector_kind=connector_kind,
    )
    if not removed:
        msg = f"no project-specific credential for {connector_kind}"
        raise NotFound(msg)


@router.post(
    "/{project_id}/connector-credentials/{connector_kind}/rotate",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def rotate_cred(
    request: Request,
    project_id: ProjectScopeDep,
    connector_kind: str,
    body: Annotated[CredentialPlaintextIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> None:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    connector = await _connector_by_kind(session, connector_kind)
    svc = _service(request, session)
    await svc.rotate(
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        connector_id=connector.id,
        connector_kind=connector_kind,
        new_plaintext=body.plaintext,
    )
