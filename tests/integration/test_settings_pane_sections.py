"""The settings pane is the drawer's seven sections, and it must not lose one.

WS-B1 deleted `apps/web/src/components/Drawers.tsx` — 742 lines holding project
info, cost, members, the model profile, per-capability bindings, connectors and
their credentials, the action ledger, the agent-run digest and the signed-in
account. `docs/plan.md` names the risk of that workstream exactly: "porting it
piecemeal is how one of those quietly vanishes."

A section that vanishes leaves no trace. The pane still renders, the other
sections still work, and nothing anywhere reports that a setting is no longer
reachable — so this drives the real builder and asserts, per pane, which section
kinds came back.

The credential test is not decoration. An earlier draft of `_connectors_section`
matched credentials to connectors by `connector_kind`, a column
`ConnectorCredential` does not have. It type-checked (the builder's `session` is
`Any`, so the row is `Any` too) and it ran green in a browser, because the only
project it had been opened against held no credentials. The first real key would
have turned the whole pane into an error surface. That is what
`test_a_stored_credential_is_reported_as_set` exists to stop, and it fails on
the original draft.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.routes.surfaces import _build_tab_messages
from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_security.principal import Principal

pytestmark = pytest.mark.integration


def _model(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The pane's root data model — what the browser binds against."""
    return next(
        m["updateDataModel"]["value"]
        for m in reversed(messages)
        if m.get("updateDataModel", {}).get("path") == "/"
    )


def _section_kinds(messages: list[dict[str, Any]]) -> list[str]:
    return [s["kind"] for s in _model(messages)["sections"]]


def _owner(project_id: uuid.UUID) -> Principal:
    p = Principal(
        user_id=uuid.uuid4(),
        subject="test-owner",
        email="owner@example.test",
        actor_kind="user",
    )
    p.cache_role(project_id, "owner")
    return p


def _member(project_id: uuid.UUID) -> Principal:
    p = Principal(
        user_id=uuid.uuid4(),
        subject="test-member",
        email="member@example.test",
        actor_kind="user",
    )
    p.cache_role(project_id, "viewer")
    return p


async def _seed_project(maker: Callable[[], AsyncSession], project_id: uuid.UUID) -> None:
    """A real project row, because the builder reads one.

    Through the ORM rather than raw SQL: `projects.model_profile_id` is NOT
    NULL, and a hand-written INSERT that names the columns this test happens to
    know about goes red the next time a migration adds one — which is a test
    failing for a reason that has nothing to do with what it checks.
    """
    async with maker() as s:
        profile = ModelProfile(
            id=uuid.uuid4(),
            name="settings-pane-probe",
            project_id=project_id,
            is_template=False,
            bindings_jsonb={},
            created_by=uuid.uuid4(),
        )
        s.add(profile)
        await s.flush()
        s.add(
            Project(
                id=project_id,
                title="settings pane",
                description="desc",
                status="active",
                model_profile_id=profile.id,
                created_by=uuid.uuid4(),
            )
        )
        await s.commit()


async def test_the_settings_pane_carries_every_section_the_drawer_had(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Six sections, and each one is a drawer section that had to survive.

    `fields` twice — Project and Cost — then Members, the model profile (which
    absorbed `ModelProfileSection` AND `CapabilityBindings`), Connectors, and
    the plugin listing the drawer could not have had.
    """
    await _seed_project(maker, committed_project)
    async with maker() as session:
        messages = await _build_tab_messages(
            session,
            committed_project,
            "settings",
            {},
            "settings",
            principal=_owner(committed_project),
            app_state=None,
        )
    assert _section_kinds(messages) == [
        "fields",
        "fields",
        "members",
        "model_profile",
        "connectors",
        "plugins",
    ]


async def test_the_logs_pane_reports_the_hash_chain(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`GET /v1/projects/{id}/ledger/verify` had no caller anywhere in the app.

    The chain CLAUDE.md lists as a core invariant had no interface at all: the
    only way to learn it had diverged was to call the route by hand.
    """
    await _seed_project(maker, committed_project)
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "logs", {}, "logs", principal=_owner(committed_project)
        )
    section = _model(messages)["sections"][0]
    assert section["kind"] == "ledger"
    assert set(section["chain"]) >= {"ok", "count", "first_divergence_event_id", "age_seconds"}
    # The limit is STATED, not implied. A pane quietly showing the most recent N
    # is indistinguishable from one showing all of them.
    assert section["limit"] > 0


async def test_the_profile_pane_names_the_signed_in_principal(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    await _seed_project(maker, committed_project)
    principal = _owner(committed_project)
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "profile", {}, "profile", principal=principal
        )
    rows = _model(messages)["sections"][0]["rows"]
    assert {r["label"]: r["value"] for r in rows}["Email"] == principal.email


async def test_a_pane_with_no_principal_says_so_instead_of_rendering_blank(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """An empty account panel and an unauthenticated one look identical.

    `principal` is keyword-only and defaults to `None` so the existing callers
    keep working; the cost of that default is exactly this case, and it is
    stated in words rather than rendered as four blank rows.
    """
    await _seed_project(maker, committed_project)
    async with maker() as session:
        messages = await _build_tab_messages(session, committed_project, "profile", {}, "profile")
    rows = _model(messages)["sections"][0]["rows"]
    assert len(rows) == 1
    assert "could not be resolved" in rows[0]["value"]


async def test_a_stored_credential_is_reported_as_set(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The case that would have crashed the pane, driven through the builder.

    Joining credentials to connectors on a column that does not exist raises
    `AttributeError` — but only once a credential row exists, which is why a
    browser walk over a fresh project reported everything green.
    """
    await _seed_project(maker, committed_project)
    async with maker() as session:
        connector_id = (
            await session.execute(text("SELECT id, kind FROM connectors ORDER BY kind LIMIT 1"))
        ).first()
    assert connector_id is not None, "no connectors are seeded; this test would prove nothing"
    cid, kind = connector_id

    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO connector_credentials (id, project_id, connector_id, cipher_blob,"
                " cipher_scheme, key_version, created_at, updated_at, created_by)"
                " VALUES (:id, :pid, :cid, :blob, 'test', 'v1', now(), now(), :uid)"
            ),
            {
                "id": uuid.uuid4(),
                "pid": committed_project,
                "cid": cid,
                "blob": b"not-a-real-ciphertext",
                "uid": uuid.uuid4(),
            },
        )
        await s.commit()

    try:
        async with maker() as session:
            messages = await _build_tab_messages(
                session,
                committed_project,
                "settings",
                {},
                "settings",
                principal=_owner(committed_project),
                app_state=None,
            )
        connectors = next(s for s in _model(messages)["sections"] if s["kind"] == "connectors")[
            "connectors"
        ]
        by_kind = {c["kind"]: c for c in connectors}
        assert by_kind[kind]["key_state"] == "set"
        # Every other connector is `unset`, not absent — a row that vanishes
        # because it has no credential is a connector you cannot add a key to.
        assert all(c["key_state"] in ("set", "unset") for c in connectors)
        # The plaintext never appears anywhere in the surface.
        assert "not-a-real-ciphertext" not in str(_model(messages))
    finally:
        async with maker() as s:
            await s.execute(
                text("DELETE FROM connector_credentials WHERE project_id = :pid"),
                {"pid": committed_project},
            )
            await s.commit()


async def test_a_non_owner_is_not_told_whether_a_key_is_set(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`GET /connector-credentials` is owner-gated and the surface stream is not.

    Porting the drawer's key panel unconditionally would have widened who can
    see credential state, which is a security change nobody asked for and one no
    test would have noticed.
    """
    await _seed_project(maker, committed_project)
    async with maker() as session:
        messages = await _build_tab_messages(
            session,
            committed_project,
            "settings",
            {},
            "settings",
            principal=_member(committed_project),
            app_state=None,
        )
    connectors = next(s for s in _model(messages)["sections"] if s["kind"] == "connectors")
    assert connectors["connectors"], "no connectors seeded; this assertion would be vacuous"
    assert all(c["key_state"] == "unknown" for c in connectors["connectors"])
    assert all(c["status"] is None for c in connectors["connectors"])
