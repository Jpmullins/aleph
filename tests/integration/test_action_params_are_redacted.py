"""A secret submitted through an action never lands in the append-only tables.

`redact_secrets` being correct is not the property that matters — `dispatch`
CALLING it is. That distinction is not academic: three mutations that removed
the calls entirely left every unit test green, because the unit tests exercised
the function and nothing exercised the wiring. Written correctly and read by
nothing, in the fix for a security defect.

So this drives `ActionRouter.dispatch` against Postgres and reads the rows back
out of `card_actions` and `action_ledger_events`. Both are append-only: a secret
written there cannot be removed, only rotated around.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_a2ui.action_router import REDACTED, ActionRouter, CardActionRequest
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
SECRET = "sk-live-do-not-persist-me"


def _principal() -> Principal:
    return Principal(user_id=ACTOR, subject="redaction", email="r@example.test", actor_kind="user")


async def test_a_secret_param_is_redacted_in_both_tables(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    router = ActionRouter()
    seen: dict[str, Any] = {}

    async def handler(**kwargs: Any) -> dict[str, Any]:
        # The handler DOES receive the real value — it has to, or the feature
        # does not work. Redaction is at the persistence boundary, not at the
        # call.
        seen.update(kwargs)
        # The RESULT carries the secret too, and it must. `_plugin_settings_save`
        # returns the re-rendered settings surface — `updateDataModel` and all —
        # so the stored values come straight back out of the handler and into
        # `result_jsonb` and the ledger. An earlier version of this test echoed a
        # key that did not exist, so the result path was never exercised and a
        # mutation removing its redaction stayed green.
        return {"api_key": SECRET, "ok": True}

    router.register("plugin.settings.save", handler)

    request = CardActionRequest(
        action_kind="plugin.settings.save",
        surface_kind="plugins",
        card_id=None,
        target_id=None,
        target_kind=None,
        params={
            "plugin_id": str(uuid.uuid4()),
            "plugin_kind": "connector",
            "field:api_key": SECRET,
        },
    )

    async with maker() as session:
        await router.dispatch(
            session=session,
            ledger=LedgerWriter(session),
            principal=_principal(),
            project_id=committed_project,
            request=request,
        )
        await session.commit()

    async with maker() as session:
        rows = (
            await session.execute(
                sql_text(
                    "select params_jsonb::text as p, result_jsonb::text as r"
                    " from card_actions where project_id = :pid"
                ),
                {"pid": committed_project},
            )
        ).all()
        events = (
            await session.execute(
                sql_text(
                    "select payload_jsonb::text as p from action_ledger_events"
                    " where project_id = :pid and action_kind like 'a2ui.action.%'"
                ),
                {"pid": committed_project},
            )
        ).all()

    assert rows, "the action wrote no card_actions row"
    assert events, "the action wrote no ledger event"

    for row in rows:
        assert SECRET not in row.p, "the secret is in card_actions.params_jsonb"
        assert REDACTED in row.r, "the handler's result was persisted unredacted"
        assert SECRET not in row.r, "the secret is in card_actions.result_jsonb"
        assert REDACTED in row.p
    for event in events:
        assert SECRET not in event.p, "the secret is in the append-only ledger payload"

    # And the handler still got the real thing, or the redaction broke the
    # feature it was protecting.
    assert seen.get("field:api_key") == SECRET or SECRET in str(seen)
