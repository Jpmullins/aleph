"""Where a JOB's model calls go: the project's endpoint, not the deployment's.

WS-MEP-4's other half. The API request path landed first — `litellm_for_project`
in `apps/api/src/aleph_api/routes/gateway_endpoints.py` — and the worker process
was left reading `ctx["litellm_client"]`, one `LiteLLMClient` built at boot from
`LITELLM_BASE_URL` and handed to every job for every project. So a project could
write a `gateway_endpoints` row, watch the settings screen read it back
correctly, and have its ingest, embedding, research and reviewer traffic keep
going to the deployment default. The row was configurable and most of the
traffic was not, which is the same "reads back correctly, changes nothing" shape
the workstream exists to remove — reproduced on the process that spends the
most.

**One seam, not eleven.** Every job now asks the same object which gateway it is
talking to, so a job added later inherits the resolution instead of having to
remember it. `arq.py` builds one :class:`WorkerGateways` at startup and puts it
on `ctx`; a job calls `gateways(ctx).litellm(project_id)`.

**Keyed on the ENDPOINT, not the project.** Both registries underneath
(:class:`ProjectLiteLLMClients`, :class:`ProjectGatewayCatalogs`) key on
`(base_url, digest of the key)`, so two projects pointed at the same gateway
share one client, one connection pool and one discovery cache, and rotating a
key at an unchanged URL takes effect on the next job rather than whenever the
entry happens to be evicted.

**Resolution is per call, not per boot.** An operator who repoints a project at
a new gateway must not have to restart the workers to make it true — that is the
condition MEP-4 exists to remove. It costs one indexed SELECT against
`gateway_endpoints` per job, on a path that is about to make a network call to a
language model.

**A key that cannot be decrypted raises.** `GatewayEndpointService.resolve`
refuses to fall back to Settings when a row exists and its blob is unreadable,
and this module does not soften that: sending a project's traffic to the wrong
gateway under the wrong key, silently, shows up first as somebody else's bill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from aleph_models.endpoints import (
    GatewayEndpointService,
    ProjectGatewayCatalogs,
    ProjectLiteLLMClients,
    ResolvedEndpoint,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_models.discovery import GatewayCatalog

__all__ = ["GATEWAYS_KEY", "WorkerGateways", "gateways"]

#: The `ctx` key `arq.py` publishes the resolver under. Named here rather than
#: written out at twelve call sites so a rename is one edit and not a
#: `KeyError` in a job nobody runs weekly.
GATEWAYS_KEY = "gateways"


class WorkerGateways:
    """Resolve, and then cache, the model clients a project's jobs should use.

    The worker's counterpart to `routes/gateway_endpoints.py`'s
    `gateway_clients` / `gateway_catalogs`, collapsed into one object because a
    worker has no `Request` to hang two lazily-attached registries off.

    Holds no resource of its own: the HTTP transport is the `http` capability's
    shared pool, owned and closed by the kernel, and the two registries are
    bounded LRU dicts. `aclose` therefore releases references rather than
    pretending to close sockets it does not own.
    """

    def __init__(
        self,
        *,
        settings: Any,
        session_maker: Callable[[], AsyncSession],
        pricing: Any,
        http_client: httpx.AsyncClient,
        redis_client: Any = None,
    ) -> None:
        from aleph_connectors.credentials import credential_cipher

        self._settings = settings
        self._session_maker = session_maker
        # Built once. `credential_cipher` is the only cipher factory in the
        # repo (see `test_cipher_construction_sites.py`), and the master key
        # comes off Settings — never off the agent-token signing secret, which
        # rotates for unrelated reasons and would destroy every stored key.
        self._cipher = credential_cipher(
            master_key=settings.aleph_credential_master_key,
            legacy_key=settings.credential_legacy_key,
        )
        self._clients = ProjectLiteLLMClients(
            pricing=pricing,
            session_maker=session_maker,
            http_client=http_client,
            redis_client=redis_client,
        )
        self._catalogs = ProjectGatewayCatalogs(client=http_client)

    async def resolve(self, project_id: UUID) -> ResolvedEndpoint:
        """This project's endpoint: its row if it has one, else the deployment.

        `source` on the result says which of the two answered, so "nobody
        configured a gateway" stays distinguishable from "this project chose
        this gateway" — they bill the same way and mean different things.
        """
        async with self._session_maker() as session:
            return await GatewayEndpointService(session, cipher=self._cipher).resolve(
                project_id=project_id,
                fallback_base_url=self._settings.litellm_base_url,
                fallback_api_key=self._settings.insights_litellm_api_key,
            )

    async def litellm(self, project_id: UUID) -> Any:
        """The metered model client this project's job traffic goes through."""
        return self._clients.for_endpoint(await self.resolve(project_id))

    async def catalog(self, project_id: UUID) -> GatewayCatalog:
        """This project's view of what its gateway serves."""
        return self._catalogs.for_endpoint(await self.resolve(project_id))

    def catalog_for(self, resolved: ResolvedEndpoint) -> GatewayCatalog:
        """The catalog for an endpoint already resolved.

        For the one caller — `autoconfigure_profile_job` — that needs the base
        URL and the key alongside the catalog. Resolving twice would let the
        catalog and the credentials it is configured from come from two reads
        that can disagree, which is the defect `resolve_for_project` was added
        to close on the API side.
        """
        return self._catalogs.for_endpoint(resolved)

    async def aclose(self) -> None:
        """Drop every cached client. The inverse of building them."""
        await self._clients.aclose_all()


def gateways(ctx: dict[str, Any]) -> WorkerGateways:
    """The resolver `arq.py` put on `ctx`, or a diagnostic naming what is wrong.

    A bare `ctx["gateways"]` would raise `KeyError: 'gateways'` from inside a
    job, which says nothing about the boot sequence that failed to publish it —
    the same shape as the `KeyError: 'kernel'` that `_shutdown` was fixed to
    stop printing over a real `ProbeFailed`.
    """
    resolver = ctx.get(GATEWAYS_KEY)
    if resolver is None:
        msg = (
            f"no gateway resolver on the job context under {GATEWAYS_KEY!r}: "
            f"aleph_workers.arq._startup publishes it, and a job cannot decide "
            f"which gateway a project's calls go to without it"
        )
        raise RuntimeError(msg)
    return cast("WorkerGateways", resolver)
