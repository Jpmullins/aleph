"""Install a plugin so it is still there after the process that installed it.

The join between two things that must not know about each other. The kernel
mounts capabilities and has no database — `aleph-kernel`'s dependencies are
exactly `aleph-core` and `aleph-observability`, and a kernel you cannot boot
without Postgres is not a kernel. The `plugins` table is durable and knows
nothing about mounting. This is the composition root, which already depends on
both, and it is the only place the two meet.

**What this closes.** A plugin existed only as a live Python object in one
process: restart the API and it was gone, and the worker never had it. An agent
that improves itself forgot the improvement at the next deploy. That is the
product thesis failing to persist, not a missing feature.

**Order matters and is not arbitrary.** The AST gate runs FIRST, before a row is
written, so source with an import-time side effect leaves nothing behind — the
gate is what makes storing agent-authored code safe at all, and a row written
before it would be a stored payload nobody gated. Then the row and the ledger
event in ONE transaction, then the mount. A mount that fails rolls the
transaction back, so the graph and the table cannot disagree about what is
installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_db.models.plugin import Plugin
from aleph_kernel.skills import (
    skill_capability,
    skill_capability_name,
    skill_from_source,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_kernel.kernel import Kernel

_log = structlog.get_logger(__name__)

#: Ledger action kinds. `<entity>.<verb>` per the naming rule.
PLUGIN_INSTALLED = "plugin.install"
PLUGIN_DISABLED = "plugin.disable"


@dataclass(frozen=True)
class PluginDraft:
    """What an agent hands in when it wants to install something."""

    name: str
    instructions: str
    code: str = ""
    major_version: int = 1
    source_kind: str = "skill"
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    config_schema: dict[str, Any] | None = None


class PluginService:
    """Durable installs. One transaction, one ledger row, one mount."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def install(
        self,
        *,
        project_id: UUID,
        actor_id: UUID,
        draft: PluginDraft,
        ledger: Any,
        kernel: Kernel | None = None,
    ) -> Plugin:
        """Gate, record, ledger, mount — in that order.

        `kernel` is optional so a plugin can be installed by a process that does
        not run one (a migration, an import tool). The row is what makes it
        durable; mounting is what makes it live, and the two are separable.

        Raises `SkillRejected` BEFORE writing anything if the AST gate refuses
        the code. That ordering is the point: the gate is what makes storing
        agent-authored source safe, so a row written ahead of it would be a
        payload nobody checked, sitting in the database, waiting for the next
        boot to execute it.
        """
        skill = skill_from_source(draft.name, draft.instructions, draft.code)

        row = Plugin(
            id=uuid7(),
            project_id=project_id,
            name=skill.name,
            major_version=draft.major_version,
            source_kind=draft.source_kind,
            instructions=draft.instructions,
            code=draft.code or None,
            provides=list(draft.provides) or [f"skill.{skill.name}"],
            requires=list(draft.requires),
            config_schema=draft.config_schema or {},
            state="installed",
            installed_by=actor_id,
            created_by=actor_id,
        )
        self._session.add(row)
        await self._session.flush()

        await ledger.append(
            project_id=project_id,
            actor_id=actor_id,
            actor_kind="agent",
            action_kind=PLUGIN_INSTALLED,
            target_id=row.id,
            target_kind="plugin",
            payload={
                "name": skill.name,
                "major_version": draft.major_version,
                "source_kind": draft.source_kind,
                # The SOURCE is not in the payload. The ledger is append-only,
                # and a plugin's code can be rewritten; keeping a copy here
                # would make every revision permanent and unreviewable. The row
                # holds the current source and can be updated.
                "has_code": bool(draft.code),
            },
            trace_id=None,
        )

        if kernel is not None:
            # Inside the transaction on purpose. A mount that fails rolls the
            # row back, so the graph and the table cannot disagree about what is
            # installed — which is the state that would make the next boot fail
            # for a reason nobody could see in either place alone.
            kernel.register_dynamic(skill_capability(skill, requires=frozenset(draft.requires)))

        return row

    async def reconstitute(
        self, *, project_id: UUID, kernel: Kernel
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Mount every installed plugin onto a fresh kernel. Returns (mounted, failed).

        This is what makes a restart survivable, and what the worker process
        needs in order to have ever known about a plugin at all.

        A plugin that fails to mount does NOT stop the others and does not raise.
        One bad row must not prevent a process from starting — that is precisely
        the failure `Kernel.unregister` was added for, one bad leftover making
        every subsequent install fail for the life of the process. It is
        recorded as `failed` with its reason so the agent can see its own
        graveyard rather than reinstalling the same broken thing in a loop.
        """
        rows = list(
            (
                await self._session.execute(
                    select(Plugin).where(
                        Plugin.project_id == project_id,
                        Plugin.state == "installed",
                    )
                )
            )
            .scalars()
            .all()
        )

        mounted: list[str] = []
        failed: list[tuple[str, str]] = []
        for row in rows:
            try:
                skill = skill_from_source(row.name, row.instructions, row.code or "")
                kernel.register_dynamic(
                    skill_capability(skill, requires=frozenset(row.requires or ()))
                )
            except Exception as exc:
                # `SkillRejected` is caught by inheritance and deliberately
                # not named separately: a plugin the gate now refuses is
                # exactly as unmountable as one whose helper blew up, and
                # the reason belongs in the row either way.
                reason = f"{type(exc).__name__}: {exc}"[:2048]
                row.state = "failed"
                row.failure_reason = reason
                failed.append((row.name, reason))
                _log.warning("plugin.reconstitute_failed", name=row.name, reason=reason)
                continue
            mounted.append(row.name)
        return mounted, failed

    async def disable(
        self,
        *,
        project_id: UUID,
        actor_id: UUID,
        name: str,
        ledger: Any,
        kernel: Kernel | None = None,
        force: bool = False,
    ) -> Plugin | None:
        """Stop mounting it, and keep the row so it can be turned back on.

        Deleting would lose the source an agent wrote, which is the one thing
        here that cannot be regenerated.

        `kernel`, like on `install`, is the seam to the live process — and here
        it is also the GUARDRAIL. The kernel refuses to retire a capability that
        other mounted capabilities are standing on, and that refusal is the
        product thesis: an agent may rearrange its own abilities and may not
        saw through the branch. Without passing one, this method marks a row
        `disabled` while the capability stays mounted and serving, so the
        database and the running process disagree about what the system can do.

        The HTTP route and the agent tool both go through `PluginApi.disable`
        first, so the guardrail held on the paths a user reaches. It did not
        hold HERE, and a service method whose safety depends on every caller
        remembering to check first is not a guardrail — it is a convention.

        Order matters: the kernel is asked FIRST, so a refusal leaves no row
        edit to undo. `force` is passed through, and the kernel still refuses
        when the collateral includes a protected capability — an operator may
        accept breaking their own plugins; nobody may take down the kernel's
        own footing.
        """
        row = (
            await self._session.execute(
                select(Plugin).where(
                    Plugin.project_id == project_id,
                    Plugin.name == name,
                    Plugin.state != "disabled",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None

        if kernel is not None:
            # The CAPABILITY name, not the row name. A plugin row called
            # `base-notes` is mounted as `skill.base-notes`, and looking it up
            # by the row name returns None — which reads as "core, nothing to
            # retire" and skips the guardrail entirely.
            key = skill_capability_name(name)
            plugin_id = kernel.plugin_id_for(key)
            # `is_provided` is ACTIVE, not merely registered. Skipping an
            # already-torn-down capability makes this idempotent, which is what
            # lets the HTTP route and the agent tool pass the kernel even
            # though both already went through `PluginApi.disable`. Without it,
            # the second deactivate of a plugin WITH dependents recomputes the
            # blast radius against unchanged specs and raises — turning a
            # successful forced removal into an error after the fact.
            if plugin_id is not None and kernel.is_provided(key):
                # Raises `DependentsWouldBreak` — the caller sees exactly what
                # would stop, which is the whole point of a predictable refusal.
                await kernel.deactivate(plugin_id, force=force)

        row.state = "disabled"
        await ledger.append(
            project_id=project_id,
            actor_id=actor_id,
            actor_kind="agent",
            action_kind=PLUGIN_DISABLED,
            target_id=row.id,
            target_kind="plugin",
            payload={"name": name},
            trace_id=None,
        )
        return row

    async def installed(self, *, project_id: UUID) -> Sequence[Plugin]:
        return list(
            (
                await self._session.execute(
                    select(Plugin)
                    .where(Plugin.project_id == project_id)
                    .order_by(Plugin.name, Plugin.major_version)
                )
            )
            .scalars()
            .all()
        )
