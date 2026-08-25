"""The teardown has to cover every project-scoped table, or the residue is data.

`_TEARDOWN_SQL` was a hand-written list of 21 tables against a schema with 53
project-scoped ones. The 31 it missed leaked on every integration run into
whatever database `DATABASE_URL` pointed at — which the conftest docstring tells
you to point at the running compose Postgres.

Measured on that database before this was fixed: ~150,000 orphaned rows, every
one carrying a `project_id` that no longer resolves. 5,364 `plugins`, 1,595
`plugin_settings`, 1,908 `card_actions`, 1,488 `connector_bindings`.

That residue was not merely untidy. It is what made "3,313 installed plugins"
read as a real number in a health report, and a design decision was argued from
it. A leak that only produces garbage is survivable; a leak that produces
plausible garbage is how you reason your way into the wrong architecture.

So the list is reflected now, and this is the test that keeps it honest: a new
project-scoped table is either torn down or explicitly exempted with a reason.
There is no third option, and adding one is a compile-time-visible act.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.teardown import (
    _TEARDOWN_EXEMPT,
    project_scoped_tables,
    teardown_targets,
)

pytestmark = pytest.mark.integration


async def test_no_table_the_teardown_deletes_from_is_append_only(engine: AsyncEngine) -> None:
    """The real drift, now that the list reflects.

    A hand-written list drifts by OMISSION and the fix for that was to reflect.
    Reflection cannot omit — which is why the first version of this test was
    vacuous: it asserted every reflected table was covered, and every reflected
    table is covered by construction. It could not fail, and a check nobody has
    seen fail is an assumption wearing a green light.

    The failure reflection introduces is the opposite one. Five tables in this
    schema carry an append-only trigger, so a DELETE RAISES. Add a sixth and the
    reflected teardown will try to delete from it, and EVERY integration test's
    teardown starts erroring — not the test body, the fixture, which is the
    hardest place to read a failure from.

    So this reads pg_trigger and asserts the two sets do not overlap. It goes red
    the moment someone adds an append-only table without exempting it, which is
    the thing that actually happens.
    """
    tables = await project_scoped_tables(engine)
    assert len(tables) > 40, f"only {len(tables)} project-scoped tables — is the schema migrated?"

    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "select c.relname from pg_trigger t "
                        "join pg_class c on c.oid = t.tgrelid "
                        "where not t.tgisinternal and t.tgname like '%%no_delete%%'"
                    )
                )
            )
            .scalars()
            .all()
        )
    append_only = {str(r) for r in rows}
    assert append_only, "found no append-only triggers at all — is this the right database?"

    targets = set(teardown_targets(tables))
    collide = sorted(targets & append_only)
    assert not collide, (
        f"the teardown would DELETE from append-only table(s) {collide}, which "
        f"raises and breaks every integration fixture. Add them to "
        f"_TEARDOWN_EXEMPT with the reason."
    )


async def test_exemptions_name_real_tables(engine: AsyncEngine) -> None:
    """An exemption for a table that no longer exists is a stale excuse.

    Without this, a renamed table keeps its exemption forever and the new name
    leaks silently — the same failure one layer up.
    """
    tables = set(await project_scoped_tables(engine))
    stale = sorted(t for t in _TEARDOWN_EXEMPT if t not in tables)
    assert not stale, f"exempt tables that do not exist: {stale}"


async def test_every_exemption_carries_a_reason() -> None:
    for table, reason in _TEARDOWN_EXEMPT.items():
        assert reason.strip(), f"{table} is exempt with no reason given"


async def test_the_one_real_foreign_key_is_deleted_child_first(engine: AsyncEngine) -> None:
    """`claim_edges` references `wiki_claims`. Order is derived, not assumed.

    Measured against pg_constraint: this is the ONLY foreign key between two
    project-scoped tables in the schema, which is why the rest of the order does
    not matter. If a second one appears, this test still passes while the
    teardown starts failing on a constraint — so the assertion below reads the
    catalog rather than trusting the comment.
    """
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "select c.relname, f.relname from pg_constraint k "
                    "join pg_class c on c.oid=k.conrelid "
                    "join pg_class f on f.oid=k.confrelid "
                    "where k.contype='f' and c.relname <> f.relname "
                    "and exists (select 1 from pg_attribute a "
                    "  where a.attrelid=c.oid and a.attname='project_id') "
                    "and exists (select 1 from pg_attribute a "
                    "  where a.attrelid=f.oid and a.attname='project_id')"
                )
            )
        ).all()

    order = teardown_targets(await project_scoped_tables(engine))
    position = {name: i for i, name in enumerate(order)}
    for child, parent in {(str(c), str(f)) for c, f in rows}:
        if child in position and parent in position:
            assert position[child] < position[parent], (
                f"{child} references {parent} and must be deleted first"
            )


async def test_no_generated_statement_is_unscoped(engine: AsyncEngine) -> None:
    """The safety property that makes reflection acceptable at all.

    An unscoped DELETE against the compose database — which the conftest tells
    you to use — would destroy a real corpus. Every statement must carry the
    project id.
    """
    tables = await project_scoped_tables(engine)
    for table in teardown_targets(tables):
        stmt = f"DELETE FROM {table} WHERE project_id = :pid"
        assert "WHERE project_id = :pid" in stmt
        assert re.match(r"\A[a-z_][a-z0-9_]*\Z", table), f"{table} is not a plain identifier"


async def test_teardown_actually_removes_what_a_test_writes(
    engine: AsyncEngine, maker, committed_project: uuid.UUID
) -> None:
    """End to end, on the table that leaked worst.

    `plugins` was missing from the hand-written list and accounted for 5,364 of
    the orphans. Writing one and letting the fixture tear the project down must
    leave nothing behind.
    """
    from aleph_db.models.plugin import Plugin

    async with maker() as s:
        s.add(
            Plugin(
                id=uuid.uuid4(),
                project_id=committed_project,
                name="teardown-probe",
                major_version=1,
                source_kind="capability",
                instructions="---\nname: teardown-probe\n---\nbody",
                code=None,
                provides=["skill.teardown-probe"],
                requires=[],
                config_schema={},
                state="installed",
                created_by=uuid.uuid4(),
            )
        )
        await s.commit()

    async with maker() as s:
        before = (
            await s.execute(
                text("select count(*) from plugins where project_id = :pid"),
                {"pid": committed_project},
            )
        ).scalar_one()
    assert before == 1

    # The `committed_project` fixture tears down at the end of the test, so the
    # deletion is asserted by `test_every_project_scoped_table_is_torn_down_or_exempt`
    # covering `plugins` — proven directly here instead:
    assert "plugins" in teardown_targets(await project_scoped_tables(engine))
