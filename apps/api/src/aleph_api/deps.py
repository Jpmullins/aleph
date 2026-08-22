"""FastAPI dependency providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal


def get_session_maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_maker


async def session_dep(
    maker: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_maker)],
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


# `LiteLLMDep` lives in `routes/gateway_endpoints.py`, not here.
#
# It must resolve the caller's PROJECT — `app.state.litellm` is one client built
# at boot from `LITELLM_BASE_URL`, so a project with its own `gateway_endpoints`
# row had a setting that read back correctly and routed its traffic elsewhere.
# Resolving the project needs `project_scope_dep`, and `middleware/project_scope`
# imports from this module, so defining it here is an import cycle. FastAPI
# evaluates the annotation at definition time, so deferring the import inside a
# helper does not break it either.
#
# It is one import line at each of its three call sites, and it sits next to the
# resolver it uses.


def principal_dep(request: Request) -> Principal:
    p: Principal | None = getattr(request.state, "principal", None)
    if p is None:
        from aleph_core.errors import PermissionDenied

        msg = "authentication required"
        raise PermissionDenied(msg)
    return p


PrincipalDep = Annotated[Principal, Depends(principal_dep)]
