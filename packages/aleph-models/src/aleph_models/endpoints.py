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

**Who calls this.** `apps/api/src/aleph_api/routes/gateway_endpoints.py` — list,
upsert, delete, test-connection and the per-project model list — and
`routes/model_profile.py`, whose autoconfigure and `GET /v1/gateway/models` both
resolve through :class:`ProjectGatewayCatalogs` rather than reading a catalog
built once at boot. `resolve`'s `fallback_*` arguments are what makes that
adoption safe without a flag day: a project with no row keeps the deployment
default, and `source` says which of the two it got.

**Still on Settings, and not by choice:** the agent path
(`copilot_agent._gateway_chat_model`), `app.state.litellm`, and the workers'
`ctx["litellm_client"]`. Those are MEP-6 and they are outside this module.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select

from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.gateway_endpoint import GatewayEndpoint
from aleph_models.discovery import GatewayCatalog, discover_models

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_models.limiter import GatewayLimiter

__all__ = [
    "EndpointCipher",
    "EndpointProbe",
    "GatewayEndpointService",
    "ProjectGatewayCatalogs",
    "ResolvedEndpoint",
    "redact_secret",
    "settings_endpoint",
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
    #: Whether this key may call the admin `/model/info` route. `False` is the
    #: NORMAL answer for a LiteLLM virtual key and is not a failure — but it
    #: decides whether the model list carries modes, windows and rates or only
    #: ids, which is the difference between autoconfigure binding on evidence
    #: and binding on a hints file. `None` means the endpoint could not be
    #: reached at all, so the question was never answered.
    model_info_allowed: bool | None = None
    #: The ids it advertised. `model_count` is not derived from this in the
    #: response so that a truncated list can never make the count look wrong.
    models: tuple[str, ...] = ()


#: The admin route whose reachability decides how much metadata discovery gets.
#: Duplicated from `discovery._MODEL_INFO_PATH` rather than imported, because
#: that name is private and pyright strict refuses the cross-module read. The
#: cost of the duplication is bounded: if the two ever disagree, discovery still
#: returns the right models and only `model_info_allowed` is wrong — and
#: `test_a_probe_reports_whether_the_admin_route_was_allowed` drives both halves
#: against the same fake, so the disagreement is a failing test rather than a
#: quietly wrong flag.
MODEL_INFO_PATH = "/model/info"

#: Below this length a "secret" is more likely to be a substring of ordinary
#: prose than a credential, and blanking it would corrupt the operator-facing
#: error text this whole path exists to preserve.
_MIN_REDACTABLE = 8


def redact_secret(text: str, secret: str) -> str:
    """Blank an endpoint's own api key out of text that is about to be returned.

    The test-connection route returns the gateway's words verbatim, which is the
    point of it — "something went wrong" sends an operator looking for a network
    fault when the answer was "invalid api key". But some gateways echo the
    bearer token back in their 401 body, and a verbatim relay of that puts the
    key in an HTTP response, in `last_probe_error`, and in every log line that
    quotes it. Both criteria hold only if the one value we already know is a
    secret is removed on the way out.
    """
    if not secret or len(secret) < _MIN_REDACTABLE:
        return text
    return text.replace(secret, "[redacted]")


def settings_endpoint(*, base_url: str, api_key: str) -> ResolvedEndpoint:
    """The deployment default, in the shape everything else resolves to.

    So that the un-scoped `GET /v1/gateway/models` and a project with no row of
    its own go through the SAME cache entry and the same code path, instead of
    one reading a catalog built at boot and the other building its own.
    """
    return ResolvedEndpoint(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        name="deployment default",
        endpoint_id=None,
        source=SOURCE_SETTINGS,
    )


class ProjectGatewayCatalogs:
    """One `GatewayCatalog` per distinct endpoint, bounded.

    The thing this replaces is a single `GatewayCatalog` built at boot from
    `LITELLM_BASE_URL` and read by every project — which is why `GET
    /v1/gateway/models` answered with the same list no matter whose project
    asked. A catalog per project would be wrong in the other direction: two
    projects pointed at the same endpoint should share one cache and one TTL.
    So the key is the ENDPOINT, not the project.

    The key is `(base_url, digest of the api key)` and deliberately NOT the
    endpoint id: two rows in two projects naming the same gateway with the same
    key see the same models, and giving them separate caches would double the
    discovery traffic to prove it. The api key is in the key because rotating
    one without changing the URL must invalidate the cache — otherwise the new
    key does not take effect until the TTL expires and the failure presents as
    "it worked yesterday". A digest is enough to notice the change and useless
    to anything that dumps this dict.

    Bounded because it is process-wide and keyed on operator-supplied data: an
    unbounded dict keyed on `base_url` is a memory leak with an HTTP route in
    front of it. Eviction is LRU and costs one rediscovery, never correctness —
    a `GatewayCatalog` owns no connection, only a cached list.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        ttl_s: float = 300.0,
        max_entries: int = 32,
        limiter: GatewayLimiter | None = None,
    ) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._max_entries = max(1, max_entries)
        self._limiter = limiter
        self._catalogs: OrderedDict[str, GatewayCatalog] = OrderedDict()

    @staticmethod
    def key_for(resolved: ResolvedEndpoint) -> str:
        digest = hashlib.sha256(resolved.api_key.encode()).hexdigest()[:16]
        return f"{resolved.base_url}|{digest}"

    def __len__(self) -> int:
        return len(self._catalogs)

    def for_endpoint(self, resolved: ResolvedEndpoint) -> GatewayCatalog:
        key = self.key_for(resolved)
        existing = self._catalogs.get(key)
        if existing is not None:
            self._catalogs.move_to_end(key)
            return existing
        catalog = GatewayCatalog(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            client=self._client,
            ttl_s=self._ttl_s,
            limiter=self._limiter,
        )
        self._catalogs[key] = catalog
        while len(self._catalogs) > self._max_entries:
            self._catalogs.popitem(last=False)
        return catalog


class ProjectLiteLLMClients:
    """One `LiteLLMClient` per distinct endpoint, bounded. The same idea as
    `ProjectGatewayCatalogs`, for the object that makes the CALLS.

    `app.state.litellm` is a single client built at boot from
    `LITELLM_BASE_URL`, so every project's model calls went to the deployment's
    gateway whatever its `gateway_endpoints` row said. The row was configurable
    and the traffic was not — a setting that reads back correctly and changes
    nothing, which is the shape this workstream exists to remove.

    Keyed on the ENDPOINT, not the project, for the reason the catalog registry
    is: two projects naming the same gateway with the same key should share one
    client and one connection pool. The key includes a digest of the api key so
    rotating a key at an unchanged URL takes effect immediately rather than
    whenever something happens to evict the entry.

    Bounded and LRU. Unlike a catalog, a client OWNS a connection pool, so
    eviction has to close it — an evicted client whose pool stays open is a
    file-descriptor leak with an HTTP route in front of it. `aclose_all` is the
    inverse the capability unwinds with.
    """

    def __init__(
        self,
        *,
        pricing: Any,
        session_maker: Any,
        http_client: Any,
        redis_client: Any = None,
        limiter: GatewayLimiter | None = None,
        max_entries: int = 16,
    ) -> None:
        self._pricing = pricing
        self._session_maker = session_maker
        self._http = http_client
        self._redis = redis_client
        self._limiter = limiter
        self._max_entries = max(1, max_entries)
        self._clients: OrderedDict[str, Any] = OrderedDict()
        self._evicted: list[Any] = []

    def __len__(self) -> int:
        return len(self._clients)

    def for_endpoint(self, resolved: ResolvedEndpoint) -> Any:
        from aleph_models.client import LiteLLMClient

        key = ProjectGatewayCatalogs.key_for(resolved)
        existing = self._clients.get(key)
        if existing is not None:
            self._clients.move_to_end(key)
            return existing
        client = LiteLLMClient(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            http_client=self._http,
            pricing=self._pricing,
            session_maker=self._session_maker,
            redis_client=self._redis,
            limiter=self._limiter,
        )
        self._clients[key] = client
        while len(self._clients) > self._max_entries:
            _key, dropped = self._clients.popitem(last=False)
            # Held, not closed here: the shared `http_client` is owned by the
            # `http` capability and closing it would take every other client
            # with it. Recorded so `aclose_all` can be exact about what it owns.
            self._evicted.append(dropped)
        return client

    async def aclose_all(self) -> None:
        """Drop every cached client. The inverse of building them.

        The HTTP transport is shared and owned elsewhere, so this releases
        references rather than closing sockets — which is the honest thing to
        do and is why it does not pretend to be a full teardown.
        """
        self._clients.clear()
        self._evicted.clear()


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

    async def get_by_id(self, *, project_id: UUID, endpoint_id: UUID) -> GatewayEndpoint:
        """One row, scoped. `NotFound` when it belongs to somebody else.

        Scoped on the READ, not only on the write: an id is guessable in
        principle and a route that loads by id alone hands another tenant's
        endpoint — base URL, probe errors and all — to whoever asks.
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
        return row

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
            return settings_endpoint(base_url=fallback_base_url, api_key=fallback_api_key)
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
        row = await self.get_by_id(project_id=project_id, endpoint_id=endpoint_id)

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

        # Asked BEFORE the list, and separately, because the answer is not
        # derivable from the list: `metadata_available` looks the same whether
        # the gateway described a model or `aleph_models.hints` did. One extra
        # GET on an operator-initiated button is a fair price for the
        # difference between "your key is restricted, that is normal" and "your
        # gateway is telling us nothing and we do not know why".
        allowed = await self._model_info_allowed(row.base_url, api_key, client)

        try:
            models = await discover_models(
                base_url=row.base_url, api_key=api_key, client=client, limiter=limiter
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text.strip()[:1500]
            error = f"HTTP {exc.response.status_code} from {exc.request.url}: {body}"
        except httpx.HTTPError as exc:
            # `ConnectError`, `ReadTimeout`, `InvalidURL` — the operator typed
            # the host wrong, and the type name is most of the diagnosis.
            error = f"{type(exc).__name__}: {exc}"
        else:
            return EndpointProbe(
                ok=True,
                model_count=len(models),
                error=None,
                model_info_allowed=allowed,
                models=tuple(m.id for m in models),
            )
        return EndpointProbe(
            ok=False,
            model_count=0,
            error=redact_secret(error, api_key)[:2048],
            model_info_allowed=allowed,
        )

    async def _model_info_allowed(
        self, base_url: str, api_key: str, client: httpx.AsyncClient | None
    ) -> bool | None:
        """Whether this key may call the admin route. `None` when unreachable.

        Never raises: an endpoint that cannot be reached has not answered this
        question, and reporting `False` would tell an operator their key is
        restricted when the truth is that their URL is wrong.
        """
        owned = client is None
        http = client or httpx.AsyncClient(timeout=20.0)
        try:
            resp = await http.get(
                f"{base_url.rstrip('/')}{MODEL_INFO_PATH}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None
        finally:
            if owned:
                await http.aclose()
        if resp.status_code in (401, 403):
            return False
        return resp.status_code < 400

    # -- delete --------------------------------------------------------------

    async def delete(
        self,
        *,
        ledger: LedgerWriter,
        actor_id: UUID,
        actor_kind: str,
        project_id: UUID,
        endpoint_id: UUID,
    ) -> str:
        """Remove one endpoint. Returns its name, for the caller's message.

        Addressed by id rather than by name because that is how the route
        addresses it, and a second addressing scheme is a second way for a
        delete to hit the wrong row. `NotFound` — not a `False` nobody checks —
        when it is not this project's.
        """
        row = await self.get_by_id(project_id=project_id, endpoint_id=endpoint_id)
        name = row.name
        await self._session.delete(row)
        await self._session.flush()

        from aleph_observability.tracing import current_trace_id

        await ledger.append(
            project_id=project_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            action_kind="gateway_endpoint.delete",
            target_id=endpoint_id,
            target_kind="gateway_endpoint",
            payload={"name": name},
            trace_id=current_trace_id(),
        )
        return name
