"""Resolve a project's gateway endpoint, and find out whether it answers.

WS-MEP-4, the data half. `GatewayEndpoint` (`aleph_db.models.gateway_endpoint`)
makes the gateway a row instead of two process-wide environment variables; this
is the code that reads it, writes it, and asks it whether it is really there.

Three properties are the point, and each one is a defect this repository has
already shipped in some other form:

* **The key never leaves the server.** It is encrypted with the *same* cipher
  `ConnectorCredential` uses — passed in, never constructed here — so
  `ALEPH_CREDENTIAL_MASTER_KEY` rotation and `reencrypt` cover gateway keys
  with the machinery that exists. `resolve` is the only path that decrypts, and
  what it returns is consumed in process to build an HTTP client.
* **A key that cannot be opened is an error with a name.** After a rotation
  that dropped the old master key, a row's blob is unreadable. Returning the
  settings fallback there would silently send a project's traffic to the wrong
  gateway with the wrong key, and the only symptom would be somebody else's
  bill. It raises, naming the endpoint and its `key_version`.
* **Configured is not reachable.** `probe` records what the endpoint actually
  said, in the endpoint's own words, and `last_probe_model_count` is stored
  separately from `last_probe_ok` because a reachable gateway advertising zero
  models is a real and bad answer that a boolean hides.

**No production caller yet.** The routes and the settings pane are the rest of
MEP-4/5; the running processes still take their endpoint from Settings, and
`resolve`'s `fallback_*` arguments exist so the first caller can adopt the row
without a flag day — a project with no row keeps the deployment default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select

from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.gateway_endpoint import GatewayEndpoint
from aleph_models.discovery import discover_models

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_models.limiter import GatewayLimiter

__all__ = [
    "EndpointCipher",
    "EndpointProbe",
    "GatewayEndpointService",
    "ResolvedEndpoint",
]

_log = structlog.get_logger(__name__)

#: What `resolve` reports when no row existed and the deployment default was
#: used. Distinguishable on purpose: "this project chose this gateway" and
#: "nobody has configured one" bill the same way and mean different things.
SOURCE_ROW = "row"
SOURCE_SETTINGS = "settings"


class EndpointCipher(Protocol):
    """Structural view of `aleph_connectors.credentials.CredentialCipher`.

    A Protocol rather than an import: `aleph-connectors` sits *above*
    `aleph-models` in the dependency DAG, so importing it here would invert the
    graph. Declaring the shape keeps the one cipher implementation and one
    rotation story while leaving the arrow pointing the right way.
    """

    scheme: str
    key_version: str

    def encrypt(self, *, project_id: UUID, plaintext: str) -> bytes: ...

    def decrypt(self, *, project_id: UUID, cipher_blob: bytes, key_version: str) -> str: ...


@dataclass(frozen=True)
class ResolvedEndpoint:
    """Where to send a call, and what to send with it."""

    base_url: str
    api_key: str
    name: str
    #: `None` when this came from Settings rather than from a row.
    endpoint_id: UUID | None
    source: str


@dataclass(frozen=True)
class EndpointProbe:
    """What the endpoint said when something actually asked it."""

    ok: bool
    #: How many models it advertised. Zero from a reachable gateway is a real
    #: answer — a virtual key with nothing attached — and `ok` alone hides it.
    model_count: int
    #: The gateway's own words on failure, truncated to the column width.
    #: Replacing them with "connection failed" is what sends an operator
    #: looking for a network fault when the answer was "invalid api key".
    error: str | None = None


def _endpoint_error(endpoint: GatewayEndpoint, exc: Exception) -> ValidationFailed:
    return ValidationFailed(
        f"gateway endpoint {endpoint.name!r} ({endpoint.id}) has an api key encrypted at "
        f"key_version {endpoint.key_version!r} that this deployment cannot open: {exc}. "
        f"Restore that master key, or re-enter the endpoint's api key."
    )


class GatewayEndpointService:
    """Read, write and probe `gateway_endpoints`. The only decryption path."""

    def __init__(self, session: AsyncSession, *, cipher: EndpointCipher) -> None:
        self._session = session
        self._cipher = cipher

    # -- write ---------------------------------------------------------------

    async def upsert(
        self,
        *,
        ledger: LedgerWriter,
        actor_id: UUID,
        actor_kind: str,
        project_id: UUID,
        name: str,
        base_url: str,
        api_key: str | None,
        is_default: bool = False,
    ) -> GatewayEndpoint:
        """Create or replace one endpoint, encrypting the key on the way in.

        `api_key=None` leaves an existing key untouched — an operator editing a
        base URL should not have to re-type a secret they cannot read back.
        `api_key=""` clears it, which is a different intention and has to stay
        expressible: a gateway on a private network needs no key.
        """
        cleaned_name = name.strip()
        cleaned_url = base_url.strip().rstrip("/")
        if not cleaned_name:
            msg = "gateway endpoint name is empty"
            raise ValidationFailed(msg)
        if not cleaned_url:
            msg = "gateway endpoint base_url is empty"
            raise ValidationFailed(msg)

        row = (
            await self._session.execute(
                select(GatewayEndpoint).where(
                    GatewayEndpoint.project_id == project_id,
                    GatewayEndpoint.name == cleaned_name,
                )
            )
        ).scalar_one_or_none()

        action_kind = "gateway_endpoint.update"
        if row is None:
            row = GatewayEndpoint(
                id=uuid7(),
                project_id=project_id,
                name=cleaned_name,
                base_url=cleaned_url,
                is_default=is_default,
                created_by=actor_id,
            )
            self._session.add(row)
            action_kind = "gateway_endpoint.create"
        else:
            row.base_url = cleaned_url
            row.is_default = is_default

        if api_key is not None:
            self._set_key(row, project_id=project_id, api_key=api_key)
        if is_default:
            await self._demote_other_defaults(project_id=project_id, keep=row.id)
        await self._session.flush()

        from aleph_observability.tracing import current_trace_id

        await ledger.append(
            project_id=project_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            action_kind=action_kind,
            target_id=row.id,
            target_kind="gateway_endpoint",
            # Never the key, and never the ciphertext either — an append-only
            # table is the last place a secret should be recoverable from.
            payload={
                "name": row.name,
                "base_url": row.base_url,
                "is_default": row.is_default,
                "has_api_key": row.api_key_cipher is not None,
                "key_version": row.key_version,
            },
            trace_id=current_trace_id(),
        )
        return row

    def _set_key(self, row: GatewayEndpoint, *, project_id: UUID, api_key: str) -> None:
        if not api_key:
            row.api_key_cipher = None
            row.cipher_scheme = None
            row.key_version = None
            return
        row.api_key_cipher = self._cipher.encrypt(project_id=project_id, plaintext=api_key)
        row.cipher_scheme = self._cipher.scheme
        # Moves with the blob, always. A row claiming v1 while holding v2 bytes
        # is the exact failure `key_version` exists to prevent.
        row.key_version = self._cipher.key_version

    async def _demote_other_defaults(self, *, project_id: UUID, keep: UUID) -> None:
        others = (
            (
                await self._session.execute(
                    select(GatewayEndpoint).where(
                        GatewayEndpoint.project_id == project_id,
                        GatewayEndpoint.is_default.is_(True),
                        GatewayEndpoint.id != keep,
                    )
                )
            )
            .scalars()
            .all()
        )
        for other in others:
            other.is_default = False

    # -- read ----------------------------------------------------------------

    async def list_for_project(self, project_id: UUID) -> list[GatewayEndpoint]:
        rows = (
            (
                await self._session.execute(
                    select(GatewayEndpoint)
                    .where(GatewayEndpoint.project_id == project_id)
                    .order_by(GatewayEndpoint.created_at, GatewayEndpoint.id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def resolve(
        self,
        *,
        project_id: UUID,
        name: str | None = None,
        fallback_base_url: str = "",
        fallback_api_key: str = "",
    ) -> ResolvedEndpoint:
        """The project's endpoint, decrypted, or the deployment default.

        Selection is deterministic and says so out loud when it has to guess:
        a named endpoint wins; otherwise the row marked default; otherwise the
        oldest row, with a warning, because two projects resolving differently
        on two processes because of row ordering is the kind of bug that only
        shows up in the bill.
        """
        rows = await self.list_for_project(project_id)
        if name is not None:
            chosen = next((r for r in rows if r.name == name), None)
            if chosen is None:
                msg = f"project {project_id} has no gateway endpoint named {name!r}"
                raise NotFound(msg)
        elif not rows:
            if not fallback_base_url:
                msg = (
                    f"project {project_id} has no gateway endpoint and no deployment "
                    f"default was supplied; there is nowhere to send a model call"
                )
                raise NotFound(msg)
            return ResolvedEndpoint(
                base_url=fallback_base_url.rstrip("/"),
                api_key=fallback_api_key,
                name="deployment default",
                endpoint_id=None,
                source=SOURCE_SETTINGS,
            )
        else:
            defaults = [r for r in rows if r.is_default]
            if len(defaults) == 1:
                chosen = defaults[0]
            else:
                chosen = (defaults or rows)[0]
                if len(rows) > 1:
                    _log.warning(
                        "gateway_endpoint.ambiguous_default",
                        project_id=str(project_id),
                        candidates=len(defaults) or len(rows),
                        using=chosen.name,
                    )

        return ResolvedEndpoint(
            base_url=chosen.base_url,
            api_key=self._open_key(chosen, project_id=project_id),
            name=chosen.name,
            endpoint_id=chosen.id,
            source=SOURCE_ROW,
        )

    def _open_key(self, row: GatewayEndpoint, *, project_id: UUID) -> str:
        if row.api_key_cipher is None:
            return ""
        try:
            return self._cipher.decrypt(
                project_id=project_id,
                cipher_blob=bytes(row.api_key_cipher),
                key_version=row.key_version or "",
            )
        except Exception as exc:
            # Named and raised, never swallowed into the fallback. Falling back
            # would send this project's traffic to a different gateway on a
            # different key, and the first symptom would be somebody else's
            # invoice.
            raise _endpoint_error(row, exc) from exc

    # -- probe ---------------------------------------------------------------

    async def probe(
        self,
        *,
        project_id: UUID,
        endpoint_id: UUID,
        client: httpx.AsyncClient | None = None,
        limiter: GatewayLimiter | None = None,
    ) -> EndpointProbe:
        """Ask the endpoint what it serves, and record the answer on the row.

        A row is written either way. "We tried and it refused" and "nobody has
        ever tried" are different states, and only one of them needs an
        operator; storing nothing on failure collapses them.
        """
        row = (
            await self._session.execute(
                select(GatewayEndpoint).where(
                    GatewayEndpoint.project_id == project_id,
                    GatewayEndpoint.id == endpoint_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            msg = f"gateway endpoint {endpoint_id} not found in project {project_id}"
            raise NotFound(msg)

        result = await self._ask(row, project_id=project_id, client=client, limiter=limiter)
        row.last_probe_at = utcnow()
        row.last_probe_ok = result.ok
        row.last_probe_error = result.error
        row.last_probe_model_count = result.model_count
        await self._session.flush()
        return result

    async def _ask(
        self,
        row: GatewayEndpoint,
        *,
        project_id: UUID,
        client: httpx.AsyncClient | None,
        limiter: GatewayLimiter | None,
    ) -> EndpointProbe:
        try:
            api_key = self._open_key(row, project_id=project_id)
        except ValidationFailed as exc:
            # An unopenable key is a probe FAILURE, not an exception out of the
            # probe: the operator asked "is this endpoint usable", and the
            # answer is no, for a reason worth reading.
            return EndpointProbe(ok=False, model_count=0, error=str(exc)[:2048])
        try:
            models = await discover_models(
                base_url=row.base_url, api_key=api_key, client=client, limiter=limiter
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text.strip()[:1500]
            return EndpointProbe(
                ok=False,
                model_count=0,
                error=f"HTTP {exc.response.status_code} from {exc.request.url}: {body}"[:2048],
            )
        except httpx.HTTPError as exc:
            return EndpointProbe(
                ok=False, model_count=0, error=f"{type(exc).__name__}: {exc}"[:2048]
            )
        return EndpointProbe(ok=True, model_count=len(models), error=None)

    # -- delete --------------------------------------------------------------

    async def delete(
        self,
        *,
        ledger: LedgerWriter,
        actor_id: UUID,
        actor_kind: str,
        project_id: UUID,
        name: str,
    ) -> bool:
        row = (
            await self._session.execute(
                select(GatewayEndpoint).where(
                    GatewayEndpoint.project_id == project_id,
                    GatewayEndpoint.name == name,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        target = row.id
        await self._session.delete(row)
        await self._session.flush()

        from aleph_observability.tracing import current_trace_id

        await ledger.append(
            project_id=project_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            action_kind="gateway_endpoint.delete",
            target_id=target,
            target_kind="gateway_endpoint",
            payload={"name": name},
            trace_id=current_trace_id(),
        )
        return True
