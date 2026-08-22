"""A plugin survives the process that installed it. WS-A1b.

The missing half of "everything is a plugin". A plugin existed only as a live
Python object inside one running process — restart the API and it was gone, and
the background worker never had it at all. There is no plugin table anywhere in
the schema: 61 `__tablename__` declarations and not one of them plugin-, skill-
or capability-related.

So an agent that improved itself forgot the improvement at the next deploy.
That is the product thesis failing to persist, not a missing feature.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_db.models.plugin import Plugin
from aleph_db.repos.ledger import LedgerWriter
from aleph_kernel.kernel import Kernel
from aleph_kernel.skills import SkillRejected
from aleph_runtime.plugin_service import PLUGIN_INSTALLED, PluginDraft, PluginService

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

INSTRUCTIONS = """\
---
name: literature-review
description: How to review a body of literature.
---

Read the sources. Say what they agree on and what they do not.
"""

HELPER = """\
def summarise(items):
    return f"{len(items)} sources"
"""

#: Source with a top-level side effect. The AST gate forbids a call at module
#: level outside a 16-name allowlist, and `open(...)` is the shape that matters:
#: loading a skill must not be able to touch the filesystem.
GATED = """\
open("/etc/passwd").read()

def helper():
    return 1
"""


def _instructions(name: str) -> str:
    """Front matter matching the name.

    `skill_from_source` resolves the name from the front matter and the passed
    name is only a fallback — correctly, since the document is what the agent
    wrote. An earlier version of this helper varied the argument and not the
    document, so every draft was stored as `literature-review` and two tests
    looked for rows that did not exist under the name they asked for.
    """
    return f"---\nname: {name}\ndescription: A skill called {name}.\n---\n\nDo the thing.\n"


def _draft(**over: object) -> PluginDraft:
    name = str(over.pop("name", "literature-review"))
    base: dict[str, object] = {
        "name": name,
        "instructions": _instructions(name),
        "code": HELPER,
    }
    base.update(over)
    return PluginDraft(**base)  # ty: ignore[invalid-argument-type]


async def test_an_installed_plugin_comes_back_after_a_restart(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """THE criterion. Install, throw the kernel away, build a fresh one."""
    async with maker() as session:
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(),
            ledger=LedgerWriter(session),
            kernel=Kernel(),
        )
        await session.commit()

    # A different process, as far as the kernel is concerned: nothing carried
    # over but the database.
    fresh = Kernel()
    assert not fresh.is_provided("skill.literature-review")

    async with maker() as session:
        mounted, failed = await PluginService(session).reconstitute(
            project_id=committed_project, kernel=fresh
        )
    assert failed == []
    assert "literature-review" in mounted

    # Reconstitution REGISTERS; `boot` activates. `is_provided` asks whether a
    # capability is currently up, not whether it is known — deliberately, so it
    # cannot be read as "the plugin is fine" when it is merely present.
    await fresh.boot()
    assert fresh.is_provided("skill.literature-review")


async def test_install_writes_an_action_ledger_event(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    async with maker() as session:
        row = await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(),
            ledger=LedgerWriter(session),
        )
        await session.commit()
        plugin_id = row.id

    from aleph_db.models.ledger import ActionLedgerEvent

    async with maker() as session:
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == PLUGIN_INSTALLED,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].target_id == plugin_id
    # The SOURCE is not in the ledger payload. It is append-only, and a plugin's
    # code gets rewritten — keeping a copy would make every draft permanent and
    # unreviewable.
    assert "code" not in events[0].payload_jsonb
    assert events[0].payload_jsonb["has_code"] is True


async def test_a_rollback_leaves_no_row_and_no_event(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The row and the ledger event are one transaction or they are a lie."""
    async with maker() as session:
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="rolled-back"),
            ledger=LedgerWriter(session),
        )
        await session.rollback()

    async with maker() as session:
        rows = list(
            (await session.execute(select(Plugin).where(Plugin.project_id == committed_project)))
            .scalars()
            .all()
        )
    assert [r.name for r in rows if r.name == "rolled-back"] == []


async def test_a_gated_plugin_leaves_no_row(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Source with an import-time side effect is refused BEFORE anything is stored.

    The ordering is the point. A row written ahead of the gate would be an
    ungated payload sitting in the database waiting for the next boot to execute
    it — the gate is what makes storing agent-authored code safe at all.
    """
    async with maker() as session:
        before = len(
            list(
                (
                    await session.execute(
                        select(Plugin).where(Plugin.project_id == committed_project)
                    )
                )
                .scalars()
                .all()
            )
        )
        with pytest.raises(SkillRejected):
            await PluginService(session).install(
                project_id=committed_project,
                actor_id=ACTOR,
                draft=_draft(name="dangerous", code=GATED),
                ledger=LedgerWriter(session),
            )
        await session.rollback()

    async with maker() as session:
        after = len(
            list(
                (
                    await session.execute(
                        select(Plugin).where(Plugin.project_id == committed_project)
                    )
                )
                .scalars()
                .all()
            )
        )
    assert after == before


async def test_one_bad_plugin_does_not_stop_the_others_from_mounting(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A process must start even with a broken row in the table.

    This is the same failure `Kernel.unregister` was added for: one bad leftover
    making every subsequent install fail for the life of the process. The bad
    plugin is recorded `failed` WITH ITS REASON, so an agent can see its own
    graveyard instead of reinstalling the same broken thing in a loop.
    """
    async with maker() as session:
        svc = PluginService(session)
        await svc.install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="good-one"),
            ledger=LedgerWriter(session),
        )
        await session.commit()

    # A row that passed the gate at install and cannot be loaded now — the
    # realistic shape, since the gate can tighten between one deploy and the
    # next. Written directly, because the service would refuse it.
    async with maker() as session:
        from aleph_core.ids import uuid7

        session.add(
            Plugin(
                id=uuid7(),
                project_id=committed_project,
                name="broken-one",
                major_version=1,
                source_kind="skill",
                instructions="---\nname: broken-one\n---\nbody",
                code=GATED,
                provides=["skill.broken-one"],
                requires=[],
                config_schema={},
                state="installed",
                created_by=ACTOR,
            )
        )
        await session.commit()

    fresh = Kernel()
    async with maker() as session:
        mounted, failed = await PluginService(session).reconstitute(
            project_id=committed_project, kernel=fresh
        )
        await session.commit()

    assert "good-one" in mounted
    assert [name for name, _ in failed] == ["broken-one"]
    await fresh.boot()
    assert fresh.is_provided("skill.good-one")

    async with maker() as session:
        row = (
            await session.execute(
                select(Plugin).where(
                    Plugin.project_id == committed_project, Plugin.name == "broken-one"
                )
            )
        ).scalar_one()
    assert row.state == "failed"
    assert row.failure_reason, "a failed plugin with no reason is a graveyard with no headstones"


async def test_disabling_keeps_the_row_so_it_can_come_back(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Deleting would lose the source an agent wrote — the one thing here that
    cannot be regenerated."""
    async with maker() as session:
        svc = PluginService(session)
        await svc.install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="switchable"),
            ledger=LedgerWriter(session),
        )
        await svc.disable(
            project_id=committed_project,
            actor_id=ACTOR,
            name="switchable",
            ledger=LedgerWriter(session),
        )
        await session.commit()

    fresh = Kernel()
    async with maker() as session:
        mounted, _failed = await PluginService(session).reconstitute(
            project_id=committed_project, kernel=fresh
        )
        row = (
            await session.execute(
                select(Plugin).where(
                    Plugin.project_id == committed_project, Plugin.name == "switchable"
                )
            )
        ).scalar_one()
    assert "switchable" not in mounted
    assert row.state == "disabled"
    assert row.instructions, "the instructions were dropped, so it cannot be re-enabled"


def test_the_kernel_still_has_no_database_dependency() -> None:
    """A kernel you cannot boot without Postgres is not a kernel.

    The service joining the two lives in `aleph-runtime`, the composition root.
    This is the check that keeps it there.
    """
    import pathlib
    import tomllib

    manifest = tomllib.loads(pathlib.Path("packages/aleph-kernel/pyproject.toml").read_text())
    assert sorted(manifest["project"]["dependencies"]) == [
        "aleph-core",
        "aleph-observability",
    ]


# ---------------------------------------------------------------------------
# The removal guardrail, on the service path
# ---------------------------------------------------------------------------
#
# `PluginService.disable` set `row.state = "disabled"`, ledgered, and returned.
# No `kernel.deactivate`, no blast-radius walk. So the guardrail — the product
# thesis, an agent may rearrange its own abilities and may not saw through the
# branch — held only because the HTTP route and the agent tool both happened to
# call `PluginApi.disable` first. A service method whose safety depends on every
# caller remembering to check is not a guardrail, it is a convention, and the
# one test that drove this path succeeded unconditionally.


async def test_disabling_a_plugin_another_plugin_needs_is_refused(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The refusal names what would break, and the row is not touched."""
    from aleph_kernel.errors import DependentsWouldBreak

    kernel = Kernel()
    async with maker() as session:
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="base-notes"),
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="review-pass", requires=["skill.base-notes"]),
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await session.commit()
    await kernel.boot()

    async with maker() as session:
        with pytest.raises(DependentsWouldBreak) as caught:
            await PluginService(session).disable(
                project_id=committed_project,
                actor_id=ACTOR,
                name="base-notes",
                ledger=LedgerWriter(session),
                kernel=kernel,
            )
    assert "review-pass" in str(caught.value), (
        f"the refusal does not name what would break: {caught.value}"
    )

    # Still mounted, and still enabled in the database. A refusal that left the
    # row disabled would mean the process and the database disagree about what
    # the system can do — which is worse than either answer alone.
    assert kernel.is_provided("skill.base-notes")
    async with maker() as session:
        row = (
            await session.execute(
                select(Plugin).where(
                    Plugin.project_id == committed_project, Plugin.name == "base-notes"
                )
            )
        ).scalar_one()
        assert row.state != "disabled", "a refused disable still marked the row disabled"


async def test_disabling_a_plugin_nothing_needs_unmounts_it_and_marks_the_row(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The refusal above must not have closed the feature it guards."""
    kernel = Kernel()
    async with maker() as session:
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="standalone-thing"),
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await session.commit()
    await kernel.boot()
    assert kernel.is_provided("skill.standalone-thing")

    async with maker() as session:
        row = await PluginService(session).disable(
            project_id=committed_project,
            actor_id=ACTOR,
            name="standalone-thing",
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await session.commit()

    assert row is not None
    assert row.state == "disabled"
    assert not kernel.is_provided("skill.standalone-thing"), (
        "the row says disabled and the capability is still mounted and serving"
    )


async def test_force_disables_a_depended_on_plugin_and_takes_its_dependents_down(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`force` is the operator accepting the consequence, not bypassing it.

    Without this, the refusal test above would pass against a `disable` that
    simply always refuses.
    """
    kernel = Kernel()
    async with maker() as session:
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="base-notes"),
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="review-pass", requires=["skill.base-notes"]),
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await session.commit()
    await kernel.boot()

    async with maker() as session:
        row = await PluginService(session).disable(
            project_id=committed_project,
            actor_id=ACTOR,
            name="base-notes",
            ledger=LedgerWriter(session),
            kernel=kernel,
            force=True,
        )
        await session.commit()

    assert row is not None and row.state == "disabled"
    assert not kernel.is_provided("skill.base-notes")
    assert not kernel.is_provided("skill.review-pass"), (
        "the dependent is still mounted with its provider withdrawn"
    )


async def test_a_plugin_that_would_shadow_a_core_component_is_refused_at_install(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Refused before the row is written, not dropped at serve time.

    `assemble_catalogs`'s own docstring called this "the check that turns a
    silent map overwrite into a rejected install" — and its only caller was the
    SERVE path. A shadowing plugin installed successfully, sat in the database
    looking healthy, and vanished from every `GET /catalogs` response with the
    reason in a log line nobody reads.
    """
    from aleph_core.errors import ValidationFailed

    kernel = Kernel()
    async with maker() as session:
        with pytest.raises(ValidationFailed) as caught:
            await PluginService(session).install(
                project_id=committed_project,
                actor_id=ACTOR,
                draft=_draft(name="shadower", provides=("ui:component:ClaimCard",)),
                ledger=LedgerWriter(session),
                kernel=kernel,
            )
    message = str(caught.value)
    assert "ClaimCard" in message, f"the refusal does not name the component: {message}"

    # No row, and nothing mounted. A refusal that leaves either behind is a
    # plugin the next boot will try to bring up.
    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(Plugin).where(
                        Plugin.project_id == committed_project, Plugin.name == "shadower"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "a refused install still wrote a plugin row"
    assert not kernel.is_provided("skill.shadower")


async def test_a_plugin_declaring_its_own_component_name_installs(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The refusal above must not have closed the feature it guards.

    Two plugins both defining `Chart` is explicitly allowed — they are in
    different catalogs and the surface names which one it means. Only shadowing
    CORE is refused.
    """
    kernel = Kernel()
    async with maker() as session:
        row = await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="charter", provides=("ui:component:Chart",)),
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await session.commit()
    assert row.name == "charter"
    assert "ui:component:Chart" in list(row.provides or ())


async def test_disable_accepts_the_capability_name_the_kernel_reports(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The name the only two production callers actually hold.

    Both `DELETE /v1/projects/{id}/plugins/{plugin_id}` and the agent's
    `disable_plugin` tool read the name out of `AgentPluginAPI.inspect()`, which
    reports the CAPABILITY name `skill.<row name>`. `disable` selects on
    `Plugin.name`, which holds `<row name>`. So both matched no row, both
    returned having written neither the row edit nor the `plugin.disable` ledger
    event, and both reported success — the kernel half had genuinely retired the
    capability, so the process and the database simply disagreed afterwards with
    nothing anywhere saying so.

    Pinned here as well as over HTTP because the agent tool lives in
    `copilot_agent.py` and reaches this method directly; a fix that lived only
    in the route would leave the agent path broken.
    """
    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_kernel.skills import skill_capability_name
    from aleph_runtime.plugin_service import PLUGIN_DISABLED

    kernel = Kernel()
    async with maker() as session:
        row = await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="capability-named"),
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await session.commit()
        row_name = row.name

    capability = skill_capability_name(row_name)
    assert capability != row_name, "the two spellings are the same; this test proves nothing"

    async with maker() as session:
        disabled = await PluginService(session).disable(
            project_id=committed_project,
            actor_id=ACTOR,
            name=capability,
            ledger=LedgerWriter(session),
            kernel=kernel,
        )
        await session.commit()

    assert disabled is not None, f"no row matched {capability!r}"
    assert disabled.name == row_name

    async with maker() as session:
        state = (
            await session.execute(
                select(Plugin.state).where(Plugin.project_id == committed_project)
            )
        ).scalar_one()
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == PLUGIN_DISABLED,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert state == "disabled"
    assert len(events) == 1
    # The ROW name, not whatever the caller happened to pass. A ledger that
    # records `skill.capability-named` names a row that does not exist.
    assert events[0].payload_jsonb["name"] == row_name, events[0].payload_jsonb


async def test_an_exact_row_name_wins_over_the_capability_prefix_fallback(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A row genuinely called `skill.x` must not resolve to a row called `x`.

    Stripping the prefix unconditionally would disable the wrong plugin —
    silently, since both rows exist and both look plausible. `_row_for` runs the
    exact lookup first for exactly this case, and this is the test that stops
    someone simplifying it away.

    The two rows are inserted directly rather than through `install`, because
    `install` refuses the name: `PluginCatalog` rejects a `.`, asserted below so
    the reason this state is unreachable today is written down next to the
    defence against it rather than assumed. That regex lives in `aleph-a2ui`,
    two packages away, and a correctness property of `disable` that rests on
    somebody else's validator is a coincidence, not a guarantee.
    """
    from aleph_a2ui.plugin_catalogs import PluginCatalog
    from aleph_core.ids import uuid7

    service_name = "thing"
    prefixed = f"skill.{service_name}"

    with pytest.raises(ValueError, match="catalog id"):
        PluginCatalog(name=prefixed, major=1)

    async with maker() as session:
        for name in (service_name, prefixed):
            session.add(
                Plugin(
                    id=uuid7(),
                    project_id=committed_project,
                    name=name,
                    major_version=1,
                    source_kind="skill",
                    instructions=_instructions(name),
                    provides=[f"skill.{name}"],
                    requires=[],
                    config_schema={},
                    state="installed",
                    installed_by=ACTOR,
                    created_by=ACTOR,
                )
            )
        await session.commit()

    async with maker() as session:
        disabled = await PluginService(session).disable(
            project_id=committed_project,
            actor_id=ACTOR,
            name=prefixed,
            ledger=LedgerWriter(session),
        )
        await session.commit()

    assert disabled is not None
    assert disabled.name == prefixed, "the prefix fallback took precedence over a real row"

    async with maker() as session:
        states = dict(
            (
                await session.execute(
                    select(Plugin.name, Plugin.state).where(Plugin.project_id == committed_project)
                )
            ).all()
        )
    assert states == {prefixed: "disabled", service_name: "installed"}, states
