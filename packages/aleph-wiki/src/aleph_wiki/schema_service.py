"""Read and write a project's wiki governance.

One row per project in `wiki_schemas`. The read path is get-or-create: a project
that has never had a schema set gets the shipped default rather than `None`,
because every caller — the write path, the lint, the agent — needs a schema to
do anything at all, and forcing each of them to handle "no schema yet" is how
one of them ends up skipping validation entirely.

Writes replace the schema whole. There is no patch-one-field method on purpose:
a taxonomy edited field-by-field drifts through states nobody reviewed, and one
auditable ledger event per edit is worth more than the convenience.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_wiki.models import WikiSchemaRow
from aleph_wiki.schema import WikiSchema, default_schema

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal

__all__ = ["SchemaService"]


class SchemaService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, project_id: UUID) -> WikiSchemaRow | None:
        return (
            await self._session.execute(
                select(WikiSchemaRow).where(WikiSchemaRow.project_id == project_id)
            )
        ).scalar_one_or_none()

    async def get(self, project_id: UUID) -> WikiSchema:
        """This project's schema, or the shipped default if none is stored.

        Returning the default rather than `None` is deliberate — see the module
        docstring. Nothing is written here: a project only acquires a stored
        schema when someone edits it, so the default can improve for every
        project that never customised one.
        """
        row = await self._row(project_id)
        if row is None:
            return default_schema()
        return WikiSchema.from_dict(row.payload_jsonb)

    async def is_customised(self, project_id: UUID) -> bool:
        """Whether this project has its own schema rather than the default."""
        return await self._row(project_id) is not None

    async def set(
        self,
        *,
        project_id: UUID,
        schema: WikiSchema,
        principal: Principal,
        ledger: LedgerWriter,
        trace_id: str | None = None,
    ) -> WikiSchema:
        """Replace the project's schema, in one transaction with its ledger event.

        The payload records what changed at the level that matters for review —
        which tags and categories came and went — rather than the whole document
        twice. A taxonomy losing a tag that 40 pages use is the edit worth
        seeing in an audit, and a full-document diff buries it.
        """
        row = await self._row(project_id)
        previous = WikiSchema.from_dict(row.payload_jsonb) if row is not None else default_schema()

        added_tags = sorted(set(schema.tags) - set(previous.tags))
        removed_tags = sorted(set(previous.tags) - set(schema.tags))
        added_cats = sorted(schema.category_ids - previous.category_ids)
        removed_cats = sorted(previous.category_ids - schema.category_ids)

        event = await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="wiki.schema.set",
            target_id=row.id if row is not None else None,
            target_kind="wiki_schema",
            payload={
                "domain": schema.domain,
                "tags_added": added_tags,
                "tags_removed": removed_tags,
                "categories_added": added_cats,
                "categories_removed": removed_cats,
                "min_outbound_links": schema.min_outbound_links,
                "stub_promotion_mentions": schema.stub_promotion_mentions,
                "was_default": row is None,
            },
            trace_id=trace_id,
        )

        if row is None:
            row = WikiSchemaRow(
                project_id=project_id,
                payload_jsonb=schema.to_dict(),
                created_by=principal.user_id,
                trace_id=trace_id,
                ledger_event_id=event.id,
            )
            self._session.add(row)
        else:
            row.payload_jsonb = schema.to_dict()
            row.ledger_event_id = event.id
            row.trace_id = trace_id

        await self._session.flush()
        return schema
