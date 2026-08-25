"""A plugin that survives the process that installed it.

The missing half of "everything is a plugin". Today a plugin exists only as a
live Python object inside one running process: restart the API and it is gone,
and the background worker never had it at all. There is no plugin table anywhere
in the schema — 61 `__tablename__` declarations and not one of them is plugin-,
skill- or capability-related.

Which means an agent that improves itself forgets the improvement at the next
deploy. That is not a gap in a feature; it is the product thesis failing to
persist.

**The kernel does not import this, and must not.**
`packages/aleph-kernel/pyproject.toml` declares exactly `aleph-core` and
`aleph-observability`, and a kernel that could reach a database would be a
kernel you cannot boot without one. The service that joins the two lives in
`aleph-runtime`, the composition root, which already depends on both.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns

#: What the row carries the body of.
#:
#: `capability` ONLY. This tuple read `("skill", "capability")` and the docstring
#: claimed the two "differ in what reconstitution does with them, which is why
#: the column exists". Nothing branched on it: `PluginService.install` and
#: `PluginService.reconstitute` both called `skill_from_source` unconditionally,
#: so every plugin Aleph could author was a skill and the `capability` value was
#: aspirational.
#:
#: Worse than redundant — the `skill` half was dead. Nothing ever put a plugin
#: row's `instructions` in front of a model; the instructions the assistant
#: reads come from the deepagents `StoreBackend` at `/skills/authored/`, a
#: separate and working mechanism. So the table carried durability, mounting, a
#: PluginId and a restart story for prose no model saw.
#:
#: A plugin is a kernel capability: code with a setup, an inverse, a live probe
#: and accurate provides/requires. An instruction document is a skill and does
#: not belong here. See `docs/decisions.md` D15.
SOURCE_KINDS = ("capability",)

#: Lifecycle. `installed` is durable-but-not-running: the row exists and the
#: next boot will mount it. `disabled` is deliberate — the agent turned it off
#: and the row stays so it can be turned back on. `failed` retains the reason,
#: so an agent can see its own graveyard instead of reinstalling the same broken
#: thing in a loop.
PLUGIN_STATES = ("installed", "disabled", "failed")


class Plugin(CommonColumns, Base):
    __tablename__ = "plugins"
    __table_args__ = (
        # One live plugin per name per MAJOR version per project.
        #
        # The major version is in the key because `aleph://plugin/<name>@1` and
        # `@2` are different strings and therefore different catalogs — they
        # coexist, and a surface created before an upgrade keeps painting. A
        # unique constraint on `(project_id, name)` alone would make an upgrade
        # a destructive replace.
        UniqueConstraint(
            "project_id", "name", "major_version", name="uq_plugins_project_name_major"
        ),
        Index("ix_plugins_project_state", "project_id", "state"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    major_version: Mapped[int] = mapped_column(nullable=False, server_default="1")
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: The instruction document. A skill IS this; a capability may carry one as
    #: documentation for the agent that installs it.
    instructions: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    #: Python source, AST-gated before it is ever stored. NULL for an
    #: instructions-only skill.
    code: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The kernel graph edges this plugin declares. Stored as written rather
    #: than re-derived at load: a plugin whose `requires` changed underneath it
    #: should fail to mount loudly, not mount against a different graph.
    provides: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    requires: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    #: JSON Schema for the plugin's settings, which
    #: `aleph_a2ui.settings_card.settings_components` renders.
    #:
    #: A field declaring itself a secret is REFUSED there rather than obscured —
    #: a settings value is persisted to `card_actions` and to the append-only
    #: ledger, so an api_key here would be plaintext in two permanent tables.
    #: Credentials go through `ConnectorCredential`, which encrypts.
    config_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="installed")
    #: Why it is `failed`. Retained so the agent can see what it did wrong.
    failure_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    #: The agent or person who installed it. `created_by` records who wrote the
    #: ROW; this records who asked for the plugin, and for an agent-authored one
    #: they are the same.
    installed_by: Mapped[UUID | None] = mapped_column(nullable=True)
