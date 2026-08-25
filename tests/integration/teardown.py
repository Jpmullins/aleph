"""How a committed integration fixture is torn down.

Lives outside `conftest.py` so it can be IMPORTED — by
`test_teardown_is_complete.py`, which asserts the list covers the schema. A
conftest is loaded by pytest as a plugin, not as a module on the path, so logic
that needs testing cannot live there.

The safety property, stated once: every statement generated here is scoped to a
single throwaway `project_id`. Nothing truncates. That is what makes reflecting
the table list acceptable against the compose database, which the conftest
docstring tells you to point `DATABASE_URL` at.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

#: ROWS WITH A NULL `project_id` ARE NOT ORPHANS.
#:
#: `ModelProfile` templates are the one documented exception to "every row
#: carries a project_id" (CLAUDE.md). They carry NULL, and a cleanup written as
#: `WHERE NOT EXISTS (SELECT 1 FROM projects WHERE id = x.project_id)` deletes
#: them, because NULL is not in `projects`.
#:
#: That is not hypothetical. A one-off orphan sweep on 2026-08-25 removed both
#: templates (`aleph-dev`, `aleph-production`) and every integration test that
#: boots the app went red with `ProbeFailed: probe for 'models' failed: default
#: model profile 'aleph-dev' is not seeded`. The capability probe caught it
#: immediately, which is what probes are for — but nothing stopped the delete.
#:
#: The teardown below is safe from this by construction: every statement is
#: `WHERE project_id = :pid` against a real uuid4, and NULL never equals it.
#: Any FUTURE cleanup must scope the same way, or explicitly exclude
#: `project_id IS NULL`.

#: Tables a teardown must NOT delete from, and why. Each is load-bearing.
#:
#: The first five carry an append-only trigger, so a DELETE RAISES. A fixture
#: could bypass one with `session_replication_role` — `aleph` is a superuser in
#: the compose stack — and must not: switching off a core invariant to tidy up
#: is how the invariant stops being one.
#:
#: The last four are PARENTS of those append-only tables. Their children cannot
#: be removed, so removing the parent would strand a row that is permanent by
#: design and can no longer be interpreted. Keeping them is the cost of having
#: an append-only history at all.
#:
#: NOTE: the previous version justified keeping `wiki_pages` on a foreign key
#: from `wiki_revisions`. There is no such constraint — `wiki_revisions` has no
#: foreign keys at all. The row is kept for the coherence reason above, which is
#: the real one.
_TEARDOWN_EXEMPT: dict[str, str] = {
    "action_ledger_events": "append-only trigger (ledger_no_delete)",
    "wiki_revisions": "append-only trigger (wiki_revisions_no_delete)",
    "artifact_versions": "append-only trigger (artifact_versions_no_delete)",
    "hypothesis_versions": "append-only trigger (hypothesis_versions_no_delete)",
    "interactive_card_versions": "append-only trigger (card_versions_no_delete)",
    "ledger_chain_heads": "the ledger's head pointer; goes with the ledger",
    "wiki_pages": "parent of the undeletable wiki_revisions",
    "artifacts": "parent of the undeletable artifact_versions",
    "hypotheses": "parent of the undeletable hypothesis_versions",
    "interactive_cards": "parent of the undeletable interactive_card_versions",
}

#: Deletes for rows that reach a project INDIRECTLY, so reflection cannot find
#: them: the table has no `project_id` of its own. Run before the reflected
#: sweep, because they read the parent rows the sweep is about to remove.
_INDIRECT_SQL: tuple[str, ...] = (
    "DELETE FROM agent_events WHERE agent_run_id IN"
    " (SELECT id FROM agent_runs WHERE project_id = :pid)",
    "DELETE FROM source_versions WHERE source_id IN"
    " (SELECT id FROM sources WHERE project_id = :pid)",
)

#: The one real foreign key between two project-scoped tables. The child has to
#: go first. Everything else in this schema is unconstrained, so the rest of the
#: order does not matter — measured against pg_constraint, not assumed.
_DELETE_FIRST: tuple[str, ...] = ("claim_edges",)

#: Tables with a `project_id`, discovered from the live schema. Memoised per
#: database URL.
#:
#: REFLECTED, not hand-listed, and that is a reversal. The previous comment
#: argued for an explicit list because "a truncate-everything teardown is a
#: data-loss bug waiting for the first person who points DATABASE_URL at the
#: running compose Postgres, which is the documented way to run these". The
#: danger is real and the conclusion was wrong: the safety property is that
#: every statement is scoped to ONE throwaway `project_id`, not that the table
#: names were typed by hand. A scoped DELETE cannot touch a real corpus whatever
#: table it names; a TRUNCATE could, and this never truncates.
#:
#: What the hand-written list actually bought was drift. Measured 2026-08-25 on
#: the compose database: 53 project-scoped tables, 21 named, and **31 leaking** —
#: 5,364 orphaned `plugins` rows, 1,595 `plugin_settings`, 1,908 `card_actions`,
#: 1,488 `connector_bindings`, and ~150,000 orphaned rows in total, every one of
#: them pointing at a project id that no longer resolves. That residue is what
#: made "3,313 installed plugins" look like a real number.
_REFLECTED_CACHE: dict[str, tuple[str, ...]] = {}

_IDENTIFIER = re.compile(r"\A[a-z_][a-z0-9_]*\Z")

_PROJECT_SCOPED_TABLES_SQL = """
select c.relname
from pg_class c
join pg_attribute a on a.attrelid = c.oid and a.attname = 'project_id' and a.attnum > 0
join pg_namespace n on n.oid = c.relnamespace
where c.relkind = 'r' and n.nspname = 'public'
order by c.relname
"""


async def project_scoped_tables(engine: AsyncEngine) -> tuple[str, ...]:
    """Every public table carrying a `project_id`, from the live catalog."""
    key = str(engine.url)
    cached = _REFLECTED_CACHE.get(key)
    if cached is not None:
        return cached
    async with engine.connect() as conn:
        rows = (await conn.execute(text(_PROJECT_SCOPED_TABLES_SQL))).scalars().all()
    names = tuple(str(r) for r in rows)
    _REFLECTED_CACHE[key] = names
    return names


def teardown_targets(tables: Sequence[str]) -> tuple[str, ...]:
    """The tables a teardown deletes from, in order. Pure, so it is testable."""
    deletable = [t for t in tables if t not in _TEARDOWN_EXEMPT and t != "projects"]
    first = [t for t in _DELETE_FIRST if t in deletable]
    rest = sorted(t for t in deletable if t not in first)
    return (*first, *rest)


async def teardown_project(
    engine: AsyncEngine, maker: Callable[[], AsyncSession], project_id: uuid.UUID
) -> None:
    """Remove one throwaway project and everything scoped to it.

    Every statement carries `WHERE project_id = :pid`. That invariant is the
    safety property — see the note on `_REFLECTED_CACHE` — and
    `test_teardown_is_complete.py` asserts no generated statement is unscoped.
    """
    tables = await project_scoped_tables(engine)
    async with maker() as s:
        for statement in _INDIRECT_SQL:
            await s.execute(text(statement), {"pid": project_id})
        for table in teardown_targets(tables):
            if not _IDENTIFIER.match(table):  # pragma: no cover - catalog is trusted
                msg = f"refusing to interpolate {table!r} as an identifier"
                raise ValueError(msg)
            stmt = text(f"DELETE FROM {table} WHERE project_id = :pid")
            await s.execute(stmt, {"pid": project_id})
        await s.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": project_id})
        await s.commit()
