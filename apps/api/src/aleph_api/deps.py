"""FastAPI dependency providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aleph_db.repos.ledger import LedgerWriter
from aleph_models.client import LiteLLMClient
from aleph_security.principal import Principal


def get_session_maker(request: Request) -> "async_sessionmaker[AsyncSession]":
    return request.app.state.session_maker


async def session_dep(
    maker: "async_sessionmaker[AsyncSession]" = Depends(get_session_maker),
) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(session_dep)]


def ledger_dep(session: SessionDep) -> LedgerWriter:
    return LedgerWriter(session)


LedgerDep = Annotated[LedgerWriter, Depends(ledger_dep)]


def litellm_dep(request: Request) -> LiteLLMClient:
    return request.app.state.litellm


LiteLLMDep = Annotated[LiteLLMClient, Depends(litellm_dep)]


def principal_dep(request: Request) -> Principal:
    p: Principal | None = getattr(request.state, "principal", None)
    if p is None:
        from aleph_core.errors import PermissionDenied

        msg = "authentication required"
        raise PermissionDenied(msg)
    return p


PrincipalDep = Annotated[Principal, Depends(principal_dep)]
