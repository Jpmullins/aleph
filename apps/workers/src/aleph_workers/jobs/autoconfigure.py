"""autoconfigure_profile_job: bind a new project's models from the live gateway.

Enqueued by project creation. It runs in the worker rather than inline in the
request because autoconfigure *calls every model the gateway advertises* to
check it is reachable — a network round trip per model, on a path where the
agent already runs in-process inside FastAPI. Twenty-six probes on the create
request would be twenty-six probes of user-visible latency.

Doing it at all is the fix for the defect that made retrieval unusable: the
seeded templates named an embedding model (`titan-embed-v2`) that the configured
gateway does not serve, so every project ever created inherited a binding that
could only fail. Templates now ship that capability unbound, and this job fills
it in from what is actually there.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_core.errors import ValidationFailed
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.autoconfigure import autoconfigure_project
from aleph_observability.tracing import current_trace_id, start_span

_log = structlog.get_logger(__name__)


async def autoconfigure_profile_job(ctx: dict[str, Any], project_id_str: str) -> dict[str, Any]:
    maker = ctx["session_maker"]
    settings = ctx["settings"]
    catalog = ctx["gateway_catalog"]
    http_client = ctx["gateway_http"]
    pid = UUID(project_id_str)

    with start_span("worker.autoconfigure_profile", **{"aleph.project_id": project_id_str}):
        async with maker() as session:
            project = (
                await session.execute(select(Project).where(Project.id == pid))
            ).scalar_one_or_none()
            if project is None:
                return {"ok": False, "reason": "project not found"}
            actor_id = project.created_by

            try:
                profile, outcome = await autoconfigure_project(
                    session,
                    project_id=pid,
                    catalog=catalog,
                    base_url=settings.litellm_base_url,
                    api_key=settings.insights_litellm_api_key,
                    http_client=http_client,
                    probe=True,
                )
            except ValidationFailed as exc:
                # A gateway that serves nothing usable is an operator problem,
                # not a crash: the profile keeps whatever it had, the capability
                # stays unbound, and the reason is on the record rather than in
                # a traceback nobody reads.
                _log.warning(
                    "worker.autoconfigure.no_bindings", project_id=project_id_str, reason=str(exc)
                )
                return {"ok": False, "reason": str(exc)}

            await LedgerWriter(session).append(
                project_id=pid,
                actor_id=actor_id,
                actor_kind="system",
                action_kind="model_profile.autoconfigure",
                target_id=profile.id,
                target_kind="model_profile",
                payload={
                    "bound": outcome.bound,
                    "unbound": outcome.unbound,
                    "unreachable": sorted(outcome.unreachable),
                    "probed": True,
                    "trigger": "project.create",
                },
                trace_id=current_trace_id(),
            )
            await session.commit()

    _log.info(
        "worker.autoconfigure.done",
        project_id=project_id_str,
        bound=len(outcome.bound),
        unbound=outcome.unbound,
    )
    return {"ok": True, "bound": outcome.bound, "unbound": outcome.unbound}
