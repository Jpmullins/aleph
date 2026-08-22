"""ActionRouter — single dispatch chokepoint for every CardAction.

Every analyst interaction with an A2UI card produces a `CardAction` row
through this router. The router:

1. Validates action params against the catalog action schema.
2. Resolves the action_kind to a registered handler.
3. Runs the handler inside a single transaction; the handler writes its
   ledger event(s) and the router writes the `CardAction` row pointing
   at the same ledger event.
4. Returns the handler result as JSON suitable for the renderer to apply.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from aleph_a2ui.catalog import CATALOG
from aleph_a2ui.models import CardAction
from aleph_core.errors import ValidationFailed
from aleph_core.ids import uuid7
from aleph_observability.tracing import current_trace_id, start_span

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


#: Field names whose VALUE must never reach an append-only table.
#:
#: Every dispatched action's params are persisted to `card_actions.params_jsonb`
#: AND to the ledger payload, and the handler's result is persisted too. Both
#: are append-only, so a secret written there cannot be deleted — only the
#: encryption key can be rotated, and these values are not encrypted.
#:
#: `_CONNECTOR_CONFIG_SCHEMA` avoids the problem by not declaring a credential
#: field, and a comment there explains why. That is a comment, not a check: the
#: moment a PLUGIN declares its own config schema (WS-A1b) nothing stops it
#: carrying an api_key — and `settings_card.py` advertises support for exactly
#: that, emitting `variant: "obscured"` for `format: "password"`. Obscured on
#: screen; plaintext in two permanent tables.
#:
#: Matched on the KEY, conservatively and by substring, because the alternative
#: is trusting whoever wrote the schema to have thought about it. A false
#: positive costs a redacted audit field; a false negative is unrecoverable.
_SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "client_secret",
    "access_key",
    "auth",
)

#: What a redacted value is replaced with. A marker, not an empty string: an
#: auditor has to be able to tell "a secret was submitted and withheld" from
#: "the field was blank".
REDACTED = "[redacted]"


def redact_secrets(value: object, *, _depth: int = 0) -> object:
    """A copy of `value` with anything secret-shaped replaced by `REDACTED`.

    Recursive, because a settings payload nests. Depth-bounded because a
    hand-built dict can be cyclic and an audit write must not hang.
    """
    if _depth > 8:
        return value
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            key = str(k).lower()
            if any(marker in key for marker in _SECRET_KEY_MARKERS):
                out[k] = REDACTED
            else:
                out[k] = redact_secrets(v, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [redact_secrets(v, _depth=_depth + 1) for v in value]
    return value


@dataclass(frozen=True)
class CardActionRequest:
    surface_kind: str
    action_kind: str
    card_id: UUID | None
    target_id: UUID | None
    target_kind: str | None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CardActionResult:
    action_id: UUID
    ok: bool
    result: dict[str, Any]


HandlerSig = Callable[..., Awaitable[dict[str, Any]]]


class ActionRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, HandlerSig] = {}

    def register(self, action_kind: str, handler: HandlerSig) -> None:
        if action_kind not in CATALOG["actions"]:
            msg = f"action_kind {action_kind!r} not in catalog"
            raise ValueError(msg)
        self._handlers[action_kind] = handler

    def registered_actions(self) -> list[str]:
        return sorted(self._handlers.keys())

    async def dispatch(
        self,
        *,
        session: AsyncSession,
        ledger: LedgerWriter,
        principal: Principal,
        project_id: UUID,
        request: CardActionRequest,
    ) -> CardActionResult:
        # Validate params against catalog action schema.
        action_spec = CATALOG["actions"].get(request.action_kind)
        if action_spec is None:
            msg = f"unknown action_kind: {request.action_kind!r}"
            raise ValidationFailed(msg)
        from jsonschema import ValidationError, validate

        try:
            validate(request.params, action_spec["params"])
        except ValidationError as exc:
            msg = f"action params invalid: {exc.message}"
            raise ValidationFailed(msg) from exc

        handler = self._handlers.get(request.action_kind)
        if handler is None:
            msg = f"no handler registered for action_kind: {request.action_kind}"
            raise ValidationFailed(msg)

        with start_span(
            "a2ui.action",
            **{
                "aleph.project_id": str(project_id),
                "aleph.surface_kind": request.surface_kind,
                "aleph.action_kind": request.action_kind,
            },
        ):
            kwargs: dict[str, Any] = {
                "session": session,
                "ledger": ledger,
                "principal": principal,
                "project_id": project_id,
                "request": request,
            }
            # Drop kwargs the handler doesn't accept.
            sig = inspect.signature(handler)
            accepted = {
                k: v
                for k, v in kwargs.items()
                if k in sig.parameters
                or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            }
            result = await handler(**accepted)

            event = await ledger.append(
                project_id=project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind=f"a2ui.action.{request.action_kind}",
                target_id=request.target_id,
                target_kind=request.target_kind,
                payload={
                    "surface_kind": request.surface_kind,
                    "card_id": str(request.card_id) if request.card_id else None,
                    # Redacted on the way INTO the ledger, not on the way out.
                    # The ledger is append-only and hash-chained: a secret
                    # written here cannot be removed afterwards.
                    "params": redact_secrets(request.params),
                    "result": redact_secrets(result),
                },
                trace_id=current_trace_id(),
            )
            action_id = uuid7()
            session.add(
                CardAction(
                    id=action_id,
                    project_id=project_id,
                    card_id=request.card_id,
                    surface_kind=request.surface_kind,
                    action_kind=request.action_kind,
                    target_id=request.target_id,
                    target_kind=request.target_kind,
                    params_jsonb=redact_secrets(request.params),
                    result_jsonb=redact_secrets(result),
                    actor_id=principal.user_id,
                    actor_kind=principal.actor_kind,
                    ledger_event_id=event.id,
                    trace_id=current_trace_id(),
                )
            )
            await session.flush()
            return CardActionResult(action_id=action_id, ok=True, result=result)
