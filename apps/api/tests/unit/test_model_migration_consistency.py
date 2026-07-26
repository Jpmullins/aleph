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
        "aleph_datasets.models",
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


def test_migration_chain_is_linear_and_single_headed() -> None:
    """One head, no orphans — a split chain makes `alembic upgrade head` ambiguous."""
    revs: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rev = down = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            if not isinstance(node.value, ast.Constant):
                continue
            if node.target.id == "revision":
                rev = node.value.value
            elif node.target.id == "down_revision":
                down = node.value.value
        if rev:
            revs[rev] = down

    parents = [d for d in revs.values() if d is not None]
    assert len(parents) == len(set(parents)), (
        f"two revisions share a parent — the chain is forked: "
        f"{sorted({p for p in parents if parents.count(p) > 1})}"
    )
    heads = set(revs) - set(parents)
    assert len(heads) == 1, f"expected exactly one head, found {sorted(heads)}"
    missing = {d for d in parents if d not in revs}
    assert not missing, f"down_revision(s) reference unknown revisions: {sorted(missing)}"
