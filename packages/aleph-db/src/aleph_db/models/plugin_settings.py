"""Where a plugin's configuration is stored.

WS-A4. `settings_card.py` is 279 lines of working, unit-tested generator that
turns a JSON Schema into a settings screen, and it had zero importers outside
its own tests — the "ship a consumer with every producer" gap CLAUDE.md names as
this codebase's dominant defect, in the module that makes the plugin thesis
visible to a person.

A plugin nobody can configure is one you trust blindly or edit `.env` for.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class PluginSettings(CommonColumns, Base):
    __tablename__ = "plugin_settings"
    __table_args__ = (
        UniqueConstraint("project_id", "plugin_id", name="uq_plugin_settings_project_plugin"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    #: The capability NAME, not a uuid. A contribution is keyed by the thing it
    #: configures, and a core capability has no uuid at all — the same
    #: addressability rule the kernel enforces, applied here so a settings row
    #: can exist for a capability that has no plugin id.
    plugin_id: Mapped[str] = mapped_column(nullable=False)

    #: The submitted values, as the schema shaped them.
    #:
    #: NO SECRETS reach here. `settings_card.settings_components` refuses a
    #: field declaring itself one (`format: password`, `writeOnly: true`), and
    #: `action_router.redact_secrets` scrubs secret-shaped keys before anything
    #: is persisted. Credentials go through `ConnectorCredential`, which
    #: encrypts; this column does not.
    values: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
