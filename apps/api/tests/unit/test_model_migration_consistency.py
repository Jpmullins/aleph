"""The ORM metadata and the migration chain must agree about what exists.

`alembic check` is the real guard for this, but it needs a live Postgres. These
tests cover the half that can be checked statically and that a schema-removal
change is most likely to get wrong: dropping a model but forgetting the
migration, or writing the migration but leaving the model behind.

They are deliberately written against the *accumulated* migration DDL rather
than a single revision, because a table can be created in one revision and
dropped in a later one — which is exactly the shape of the budgets removal.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
VERSIONS = REPO_ROOT / "apps" / "api" / "alembic" / "versions"

#: Removed 2026-07-26. Budgets were never load-bearing: the only enforcement
#: site in the repo was the OWNER-gated smoke-test route, and no production LLM
#: path ever consulted a cap.
REMOVED_TABLES = {"budgets"}
REMOVED_COLUMNS = {("projects", "budget_id")}


def test_versions_directory_was_actually_found() -> None:
    """Guard the guard: a wrong path would make every migration test vacuous."""
    assert VERSIONS.is_dir(), f"alembic versions dir not found at {VERSIONS}"
    assert len(list(VERSIONS.glob("*.py"))) >= 10, "suspiciously few migrations found"


def _migration_calls(name: str) -> list[ast.Call]:
    """Every ``op.<name>(...)`` call across the whole versions directory."""
    calls: list[ast.Call] = []
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == name
            ):
                calls.append(node)
    return calls


def _first_str_arg(call: ast.Call) -> str | None:
    for a in call.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    return None


def _orm_metadata():
    import aleph_db.models  # noqa: F401  (registers the core models)
    from aleph_db.base import Base

    # Domain packages register their own tables on the same Base.
    for mod in (
        "aleph_rks.models",
        "aleph_wiki.models",
        "aleph_artifacts.models",
        "aleph_hypotheses.models",
        "aleph_notes.models",
        "aleph_reviewer.models",
        "aleph_connectors.models",
        "aleph_evals.models",
    ):
        with contextlib.suppress(ImportError):  # optional package
            __import__(mod)
    return Base.metadata


@pytest.mark.parametrize("table", sorted(REMOVED_TABLES))
def test_removed_table_absent_from_orm(table: str) -> None:
    assert table not in _orm_metadata().tables, (
        f"table {table!r} was removed but a model still declares it — "
        f"`alembic check` would report drift."
    )


@pytest.mark.parametrize(("table", "column"), sorted(REMOVED_COLUMNS))
def test_removed_column_absent_from_orm(table: str, column: str) -> None:
    md = _orm_metadata()
    assert table in md.tables, f"{table!r} unexpectedly missing from ORM metadata"
    cols = set(md.tables[table].columns.keys())
    assert column not in cols, (
        f"{table}.{column} was removed but the model still declares it — "
        f"`alembic check` would report drift."
    )


@pytest.mark.parametrize("table", sorted(REMOVED_TABLES))
def test_removed_table_is_dropped_by_a_migration(table: str) -> None:
    """The model is gone; a migration must actually drop it in the DB too."""
    dropped = {_first_str_arg(c) for c in _migration_calls("drop_table")}
    assert table in dropped, (
        f"no migration drops table {table!r}. The ORM no longer declares it, so "
        f"a deployed database would keep an orphan table forever."
    )


@pytest.mark.parametrize(("table", "column"), sorted(REMOVED_COLUMNS))
def test_removed_column_is_dropped_by_a_migration(table: str, column: str) -> None:
    dropped = {
        (_first_str_arg(c), c.args[1].value)
        for c in _migration_calls("drop_column")
        if len(c.args) >= 2 and isinstance(c.args[1], ast.Constant)
    }
    assert (table, column) in dropped, f"no migration drops {table}.{column}."


def test_budget_rollup_trigger_is_dropped() -> None:
    """The trigger references `budgets`; leaving it would break cost writes.

    `cost_to_budget` fired on every INSERT into `cost_ledger_events` and updated
    `budgets.spent_usd`. If the table is dropped and the trigger is not, every
    LLM call in the system starts failing at the cost-write.
    """
    sql = " ".join(
        a.value
        for c in _migration_calls("execute")
        for a in c.args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
    )
    assert "DROP TRIGGER IF EXISTS cost_to_budget" in sql
    assert "DROP FUNCTION IF EXISTS budget_rollup" in sql


def _assigned_value(node: ast.AST, name: str) -> ast.expr | None:
    """Right-hand side of ``name = ...`` **or** ``name: T = ...``, if this node is it.

    Both spellings occur in the versions directory and neither is more correct,
    so a parser that understands only one of them silently skips migrations.
    """
    if isinstance(node, ast.AnnAssign):
        target = node.target
        if isinstance(target, ast.Name) and target.id == name:
            return node.value
        return None
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
    return None


def _parent_revisions(value: ast.expr | None) -> tuple[str, ...]:
    """`down_revision` is a string, ``None`` at the root, or a tuple on a merge."""
    if isinstance(value, ast.Constant):
        return (value.value,) if isinstance(value.value, str) else ()
    if isinstance(value, ast.Tuple | ast.List):
        return tuple(
            e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        )
    return ()


def _revision_graph() -> dict[str, tuple[str, ...]]:
    """revision id -> its parent revision ids, across the whole versions directory."""
    graph: dict[str, tuple[str, ...]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rev: str | None = None
        parents: tuple[str, ...] = ()
        for node in ast.walk(tree):
            declared = _assigned_value(node, "revision")
            if isinstance(declared, ast.Constant) and isinstance(declared.value, str):
                rev = declared.value
            down = _assigned_value(node, "down_revision")
            if down is not None:
                parents = _parent_revisions(down)
        assert rev is not None, f"{path.name} declares no `revision` identifier"
        graph[rev] = parents
    return graph


def test_every_migration_file_is_visible_to_the_parser() -> None:
    """Guard the guard.

    This parser used to understand only annotated assignments
    (``revision: str = "x"``), so migrations written as a bare ``revision = "x"``
    were invisible to it. That is not a hypothetical: a two-headed chain once
    passed the single-head check below because *both* revisions that forked it
    were written in the unannotated style and neither was ever parsed.

    Counting parsed revisions against files on disk makes the whole file the
    unit of coverage, so no spelling of the assignment can hide a revision.
    """
    files = sorted(VERSIONS.glob("*.py"))
    graph = _revision_graph()
    assert len(graph) == len(files), (
        f"parsed {len(graph)} revisions from {len(files)} migration files — the "
        f"parser is blind to at least one file, which makes every chain "
        f"assertion in this module vacuous."
    )


def test_migration_chain_is_single_headed() -> None:
    """One head, or `alembic upgrade head` is ambiguous and refuses to run.

    Forks are legitimate — two branches each grow a chain off one parent — but
    every fork must be reconciled by a merge revision, and "exactly one head"
    is precisely the statement that they all have been.
    """
    graph = _revision_graph()
    parents = {p for ps in graph.values() for p in ps}
    heads = sorted(set(graph) - parents)
    assert len(heads) == 1, (
        f"expected exactly one head, found {heads}. Reconcile them with "
        f"`alembic merge -m '<why>' {' '.join(heads)}`."
    )


def test_every_down_revision_exists() -> None:
    graph = _revision_graph()
    missing = sorted({p for ps in graph.values() for p in ps if p not in graph})
    assert not missing, f"down_revision(s) reference unknown revisions: {missing}"


def test_every_revision_is_reachable_from_a_single_root() -> None:
    """A revision nothing chains onto is dead code that will never be applied."""
    graph = _revision_graph()
    roots = sorted(rev for rev, parents in graph.items() if not parents)
    assert len(roots) == 1, f"expected exactly one root revision, found {roots}"

    children: dict[str, list[str]] = {rev: [] for rev in graph}
    for rev, parents in graph.items():
        for parent in parents:
            if parent in children:
                children[parent].append(rev)

    seen: set[str] = set()
    stack = [roots[0]]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children[current])

    orphans = sorted(set(graph) - seen)
    assert not orphans, f"revisions unreachable from the root {roots[0]!r}: {orphans}"
