"""Bind a project's capabilities to models the configured gateway actually serves.

This is where "defaults" really come from, and it exists because the alternative
does not work. The seeded templates used to name models chosen when the code was
written — a guess about someone else's gateway. On the first real one not a
single name matched, and the embedding binding in particular (`titan-embed-v2`
against a gateway serving `titan-embed-text-v2`) took the entire retrieval
subsystem down without reporting anything.

So Aleph ships no model list. The choice is derived from the gateway's own
metadata — mode, context window, tool support, vision, price — and each model is
*called* before it is trusted, because a model list states configuration, not
reachability.

The logic lives here rather than in the HTTP route because two callers need it:
the route an operator clicks, and the worker job project creation enqueues. It
was one copy in the route; a second copy in the worker is how the two drift.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from aleph_core.errors import ValidationFailed
from aleph_core.schemas.model_profile import ModelBindingIn
from aleph_models.discovery import (
    probe_model,
    select_default_bindings,
    unbound_capabilities,
)

if TYPE_CHECKING:
    from uuid import UUID

    import httpx

    from aleph_db.models.model_profile import ModelProfile
    from aleph_models.discovery import GatewayCatalog

_log = structlog.get_logger(__name__)


def embed_model_of(bindings: dict[str, Any]) -> str | None:
    """The `embedding` binding's model name, or None when it is unbound."""
    embedding = bindings.get("embedding")
    model = embedding.get("model") if isinstance(embedding, dict) else None
    return model if isinstance(model, str) else None


@dataclass(frozen=True)
class AutoconfigureResult:
    """What was bound, what could not be, and what changed."""

    bound: dict[str, str]
    unbound: list[str]
    unreachable: dict[str, str] = field(default_factory=dict)
    embed_changed: bool = False


async def autoconfigure_bindings(
    profile: ModelProfile,
    *,
    catalog: GatewayCatalog,
    base_url: str,
    api_key: str,
    http_client: httpx.AsyncClient | None = None,
    probe: bool = True,
) -> AutoconfigureResult:
    """Rewrite ``profile.bindings_jsonb`` from what the gateway serves.

    Mutates the ORM object in place; the caller owns the transaction and the
    ledger event. Raises :class:`ValidationFailed` when the gateway advertises
    nothing, or when nothing it advertises qualifies for any capability —
    neither is a state worth writing a profile for.
    """
    models = await catalog.models(force=True)
    if not models:
        msg = "model gateway advertises no models; cannot configure a profile from it"
        raise ValidationFailed(msg)

    unreachable: dict[str, str] = {}
    if probe:
        errors = await asyncio.gather(
            *[
                probe_model(
                    base_url=base_url,
                    api_key=api_key,
                    model=m,
                    client=http_client,
                )
                for m in models
            ]
        )
        unreachable = {m.id: e for m, e in zip(models, errors, strict=True) if e is not None}

    bindings = select_default_bindings(models, unreachable=frozenset(unreachable))
    if not bindings:
        msg = "no model on this gateway qualified for any capability"
        raise ValidationFailed(msg)

    old_embed = embed_model_of(profile.bindings_jsonb)
    profile.bindings_jsonb = {
        cap: ModelBindingIn.model_validate(b).model_dump(mode="json") for cap, b in bindings.items()
    }
    new_embed = embed_model_of(profile.bindings_jsonb)
    unbound = [c.value for c in unbound_capabilities(bindings)]
    if unbound:
        # Naming them is the point. A capability left unbound raises a clear
        # error at resolution time; a capability bound to a guess fails in the
        # middle of a research run.
        _log.warning("models.autoconfigure.unbound", capabilities=unbound)
    return AutoconfigureResult(
        bound={c: b["model"] for c, b in bindings.items()},
        unbound=unbound,
        unreachable=unreachable,
        embed_changed=new_embed != old_embed,
    )


async def autoconfigure_project(
    session: Any,
    *,
    project_id: UUID,
    catalog: GatewayCatalog,
    base_url: str,
    api_key: str,
    http_client: httpx.AsyncClient | None = None,
    probe: bool = True,
) -> tuple[ModelProfile, AutoconfigureResult]:
    """Load the project's profile, autoconfigure it, flush. No commit."""
    from aleph_db.repos.model_profile import get_project_profile

    profile = await get_project_profile(session, project_id)
    if profile is None:
        msg = f"project {project_id} has no profile"
        raise ValidationFailed(msg)
    result = await autoconfigure_bindings(
        profile,
        catalog=catalog,
        base_url=base_url,
        api_key=api_key,
        http_client=http_client,
        probe=probe,
    )
    await session.flush()
    return profile, result
