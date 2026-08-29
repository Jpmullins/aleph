"""Hard-delete the rows of a soft-deleted project.

**The leak this closes.** Deleting a project is a PATCH to `status = "deleted"`
— a soft delete, which is correct product behaviour: a person who deletes a
project by mistake should be able to get it back. Nothing ever purged the rows
behind one, so a deployment accumulated every project it had ever held. Measured
2026-08-29 on this instance: 1,136 projects, of which one had content anyone
wanted, and 1.6 MILLION rows behind the rest — 336k wiki links, 265k agent
events, 176k wiki sections, 55k document chunks.

**The append-only tables are deliberately NOT purged**, and that is a real
trade rather than an oversight. `action_ledger_events`, `wiki_revisions`,
`artifact_versions` and `interactive_card_versions` carry database triggers that
refuse a DELETE, and the only way past is `session_replication_role` or
disabling the trigger — which a scheduled job must never do. An operator
clearing a machine may reasonably bypass them by hand, once, with a backup; a
job that does it on a timer has turned an invariant into a suggestion.

That leaves roughly 20% of the volume behind. It is the honest 20%: the ledger
is what makes "who did what" answerable, and a purge that quietly edited it
would make every other answer it gives untrustworthy.

**Ordered by dependency, not alphabetically.** `claim_edges` references
`wiki_claims`, the only foreign key between two project-scoped tables in this
schema — verified against `pg_constraint`, not assumed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

#: Tables a purge must not touch, and why.
#:
#: The first four refuse a DELETE at the database level. The last three are
#: their parents: removing a parent would strand a child row that is permanent
#: by design and can no longer be interpreted.
PURGE_EXEMPT: Final[dict[str, str]] = {
    "action_ledger_events": "append-only trigger; the audit record outlives the project",
    "wiki_revisions": "append-only trigger",
    "artifact_versions": "append-only trigger",
    "interactive_card_versions": "append-only trigger",
    "wiki_pages": "parent of the undeletable wiki_revisions",
    "artifacts": "parent of the undeletable artifact_versions",
    "interactive_cards": "parent of the undeletable interactive_card_versions",
}

#: The one real foreign key between two project-scoped tables. Child first.
_DELETE_FIRST: Final[tuple[str, ...]] = ("claim_edges",)

_IDENTIFIER = re.compile(r"\A[a-z_][a-z0-9_]*\Z")

_PROJECT_SCOPED = """
select c.relname
from pg_class c
join pg_attribute a on a.attrelid = c.oid and a.attname = 'project_id' and a.attnum > 0
join pg_namespace n on n.oid = c.relnamespace
where c.relkind = 'r' and n.nspname = 'public' and c.relname <> 'projects'
order by c.relname
"""


async def purgeable_tables(session: AsyncSession) -> tuple[str, ...]:
    """Project-scoped tables a purge may delete from, in dependency order."""
    rows = (await session.execute(text(_PROJECT_SCOPED))).scalars().all()
    names = [str(r) for r in rows if str(r) not in PURGE_EXEMPT]
    first = [t for t in _DELETE_FIRST if t in names]
    return (*first, *sorted(t for t in names if t not in first))


async def purge_project_rows(session: AsyncSession, *, project_id: UUID) -> dict[str, int]:
    """Delete every purgeable row for one project. Returns per-table counts.

    Does NOT delete the `projects` row: a purged project stays visible as
    deleted, so the deletion remains auditable and a second purge is a no-op
    rather than an error.
    """
    removed: dict[str, int] = {}
    for table in await purgeable_tables(session):
        if not _IDENTIFIER.match(table):  # pragma: no cover - names come from the catalog
            msg = f"refusing to interpolate {table!r} as an identifier"
            raise ValueError(msg)
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE project_id = :pid"), {"pid": project_id}
        )
        if result.rowcount:
            removed[table] = int(result.rowcount)

    # Reached through their parents rather than by a project_id of their own.
    for stmt, label in (
        (
            "DELETE FROM agent_events WHERE agent_run_id NOT IN (SELECT id FROM agent_runs)",
            "agent_events",
        ),
        (
            "DELETE FROM source_versions WHERE source_id NOT IN (SELECT id FROM sources)",
            "source_versions",
        ),
    ):
        result = await session.execute(text(stmt))
        if result.rowcount:
            removed[label] = removed.get(label, 0) + int(result.rowcount)
    return removed
