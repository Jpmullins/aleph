"""Tool binding for the native research loop (WP-3 §2).

Resolves the project's enabled connectors (``Connector`` ⋈
``ConnectorBinding``: an explicit binding beats ``enabled_by_default``),
decrypts each kind's credential in-process via
``ConnectorCredentialService.decrypt_for_callback`` (with the local-mode
container-env fallback), and instantiates ONLY the enabled kinds — a
disallowed connector is never constructed, never bound into the graph.

The connector set lives in ``RESEARCH_CONNECTOR_FACTORIES`` and is
instantiated per-run straight from that map (the loop does not use the
global ``ConnectorRegistry`` — binding is by direct factory lookup).
"""

# pyright: reportMissingTypeStubs=false
# (first-party aleph_* packages ship inline types but no py.typed marker —
#  the same visible debt every other package in this repo carries)

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import and_, select

from aleph_connectors.arxiv import ArxivConnector
from aleph_connectors.credentials import (
    ConnectorCredentialService,
    credential_cipher,
    credential_cipher_from_env,
)
from aleph_connectors.exa import ExaConnector
from aleph_connectors.keys import legacy_read_key
from aleph_connectors.lens import LensConnector
from aleph_connectors.openalex import OpenAlexConnector
from aleph_connectors.rss import RSSConnector
from aleph_connectors.semantic_scholar import SemanticScholarConnector
from aleph_connectors.serper import SerperConnector
from aleph_connectors.tavily import TavilyConnector
from aleph_rks.models import Connector, ConnectorBinding

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_connectors.base import ConnectorBase

_log = structlog.get_logger(__name__)

ConnectorFactory = Callable[[], "ConnectorBase"]

#: The document-output research connector set (spec WP-3 §2). Factories, not
#: instances: a connector is constructed only once its binding is resolved
#: enabled — never for disabled kinds.
RESEARCH_CONNECTOR_FACTORIES: dict[str, ConnectorFactory] = {
    "tavily": TavilyConnector,
    "openalex": OpenAlexConnector,
    "arxiv": ArxivConnector,
    "semantic_scholar": SemanticScholarConnector,
    "exa": ExaConnector,
    "serper": SerperConnector,
    "rss": RSSConnector,
    "lens": LensConnector,
}

RESEARCH_CONNECTOR_KINDS: frozenset[str] = frozenset(RESEARCH_CONNECTOR_FACTORIES)

#: Container-env API keys are a DEV-ONLY convenience (local auth mode) and
#: must never be the credential source in a real deployment.
_DEV_ENV_KEYS: dict[str, str] = {
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "serper": "SERPER_API_KEY",
    "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY",
    "huggingface_hub": "HUGGINGFACE_API_KEY",
    "lens": "LENS_API_KEY",
}


def dev_credential_defaults(auth_mode: str) -> dict[str, str]:
    """The local-auth-mode env fallback map; empty under any other mode."""
    if auth_mode != "local":
        return {}
    return {kind: os.environ.get(env, "") or "" for kind, env in _DEV_ENV_KEYS.items()}


@dataclass(frozen=True)
class ConnectorRow:
    """One connector's binding state for a project."""

    kind: str
    connector_id: UUID
    requires_auth: bool
    enabled_by_default: bool
    binding_enabled: bool | None
    """The project's explicit ``ConnectorBinding.enabled``; None if unbound."""


def effective_enabled_kinds(
    rows: Sequence[ConnectorRow],
    *,
    allowed: Sequence[str] | None = None,
    registered: frozenset[str] = RESEARCH_CONNECTOR_KINDS,
) -> list[str]:
    """Explicit binding beats ``enabled_by_default``; filter to the research set."""
    out: list[str] = []
    for row in rows:
        if row.kind not in registered:
            continue
        enabled = row.binding_enabled if row.binding_enabled is not None else row.enabled_by_default
        if not enabled:
            continue
        if allowed is not None and row.kind not in allowed:
            continue
        out.append(row.kind)
    return sorted(out)


async def connector_rows(session: AsyncSession, project_id: UUID) -> list[ConnectorRow]:
    stmt = select(
        Connector.kind,
        Connector.id,
        Connector.requires_auth,
        Connector.enabled_by_default,
        ConnectorBinding.enabled,
    ).join(
        ConnectorBinding,
        and_(
            ConnectorBinding.connector_id == Connector.id,
            ConnectorBinding.project_id == project_id,
        ),
        isouter=True,
    )
    rows = (await session.execute(stmt)).all()
    return [
        ConnectorRow(
            kind=kind,
            connector_id=connector_id,
            requires_auth=bool(requires_auth),
            enabled_by_default=bool(enabled_by_default),
            binding_enabled=binding_enabled,
        )
        for kind, connector_id, requires_auth, enabled_by_default, binding_enabled in rows
    ]


async def resolve_enabled_kinds(
    session: AsyncSession,
    *,
    project_id: UUID,
    allowed: Sequence[str] | None = None,
    registered: frozenset[str] = RESEARCH_CONNECTOR_KINDS,
) -> list[str]:
    """DB-only resolution of the kinds a research run may bind (no instantiation)."""
    rows = await connector_rows(session, project_id)
    return effective_enabled_kinds(rows, allowed=allowed, registered=registered)


@dataclass(frozen=True)
class BoundTool:
    kind: str
    connector: ConnectorBase
    credential: str | None


SkipCallback = Callable[[str, str], Awaitable[None]]


async def resolve_bound_tools(
    *,
    session_maker: async_sessionmaker[AsyncSession],
    project_id: UUID,
    agent_token_secret: str,
    credential_master_key: str | None = None,
    credential_legacy_key: str = "",
    auth_mode: str = "local",
    allowed: Sequence[str] | None = None,
    factories: Mapping[str, ConnectorFactory] | None = None,
    on_skip: SkipCallback | None = None,
) -> list[BoundTool]:
    """Bind the project's enabled research connectors.

    A kind whose credential is missing or undecryptable is skipped with a
    warning (and the optional ``on_skip`` event callback), never fatal. Only
    enabled kinds are instantiated.

    ``agent_token_secret`` no longer has anything to do with the credentials —
    it is kept in the signature because callers in another workstream pass it
    positionally by keyword and it is the pre-split ``v1`` read key. The
    encryption key is ``credential_master_key``
    (``ALEPH_CREDENTIAL_MASTER_KEY``); leaving it ``None`` reads it from the
    process environment, which is what the worker callers do today because
    their own signatures do not carry it yet.
    """
    facs: Mapping[str, ConnectorFactory] = (
        factories if factories is not None else RESEARCH_CONNECTOR_FACTORIES
    )
    async with session_maker() as session:
        rows = await connector_rows(session, project_id)
        kinds = effective_enabled_kinds(rows, allowed=allowed, registered=frozenset(facs))
        row_by_kind = {r.kind: r for r in rows}
        if credential_master_key is None:
            cipher = credential_cipher_from_env()
        else:
            cipher = credential_cipher(
                master_key=credential_master_key,
                legacy_key=legacy_read_key(credential_legacy_key, agent_token_secret),
            )
        svc = ConnectorCredentialService(
            session, cipher=cipher, dev_default_for=dev_credential_defaults(auth_mode)
        )
        bound: list[BoundTool] = []
        for kind in kinds:
            row = row_by_kind[kind]
            credential: str | None = None
            if row.requires_auth:
                try:
                    credential = await svc.decrypt_for_callback(
                        project_id=project_id,
                        connector_id=row.connector_id,
                        connector_kind=kind,
                    )
                except Exception as exc:
                    _log.warning(
                        "research.tool_skipped",
                        kind=kind,
                        project_id=str(project_id),
                        reason=str(exc)[:200],
                    )
                    if on_skip is not None:
                        await on_skip(kind, str(exc)[:512])
                    continue
            bound.append(BoundTool(kind=kind, connector=facs[kind](), credential=credential))
    return bound
