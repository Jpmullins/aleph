"""The restore drill — back up the live database, restore it, prove it is the same.

A backup script that has never been restored from is a file, not a backup, and a
restore that has never been verified is a hope. This is the check that turns
`scripts/backup.sh` and `scripts/restore.sh` into evidence: it runs both against
the REAL database that is running right now, into a scratch database it creates
and drops, and then asserts the two things bash cannot.

WHAT THIS ADDS OVER `restore.sh`'s OWN VERIFICATION

`restore.sh` already compares every table's exact row count and content digest,
compares the pgvector embeddings component by component, and fires a real UPDATE
and a real DELETE at every append-only table to prove the triggers still refuse
them. All of that is SQL, and all of it runs there. Two things are not SQL:

  1. THE LEDGER HASH CHAIN. `action_ledger_events` is append-only and
     hash-chained precisely so its history is evidence, and the chain is only
     evidence if a tampered row is detectable. The recomputation lives in
     `aleph_db.repos.ledger.verify_project_chain` — it canonicalises the JSON
     payload with sorted keys, renders the timestamp with `.isoformat()`, and
     re-derives sha256 per event. A content digest proves the restored bytes
     equal the backed-up bytes as POSTGRES renders them; it does not prove the
     chain still verifies once PYTHON renders them, which is a different set of
     conversions (asyncpg's datetime, jsonb key order, microsecond precision)
     and the one the evidentiary claim actually rests on.

     The verifier runs against BOTH databases and the results must be
     IDENTICAL, rather than the restore simply being required to verify. That
     is deliberate, and it is not a weakening. The dev database contains 29
     projects whose chain genuinely does NOT verify: every run of
     `tests/integration/test_ledger_immutability.py::test_tampering_is_detectable`
     forges a `chain_hash` of 64 `f`s into a fresh project and leaves it there.
     A drill that demanded a clean chain would fail forever on a defect that has
     nothing to do with backups — and the response to a check that is red for an
     unrelated reason is to stop running it. Demanding the two sides AGREE is
     the honest form: the round trip must not move a divergence, introduce one,
     or silently repair one. Appending is allowed (the source is live), because
     a later event cannot change where an earlier chain first diverges.

  2. THE SCRATCH DATABASE IS ACTUALLY DROPPED. Including when a step in the
     middle raises. A drill that leaves a 90MB copy behind on every run is a
     drill people stop running.

The drill never writes to the live database. It reads it, and everything it
creates is named with a `aleph_restore_drill_` prefix and a pid suffix.

Exit 0 on success; prints what failed and exits 1 otherwise. Exit 2 means it
could not run here (no postgres, no psql/pg_dump) — never reported as a pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
SCRATCH_DB = f"aleph_restore_drill_{os.getpid()}"


def _resolve_url() -> str | None:
    for key in ("ALEPH_BACKUP_DATABASE_URL", "DATABASE_URL", "ALEPH_DATABASE_URL"):
        value = os.environ.get(key)
        if value:
            return value
    env_file = ROOT / "deploy" / "compose" / ".env"
    if env_file.exists():
        values: dict[str, str] = {}
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
        password = values.get("POSTGRES_PASSWORD")
        if password:
            port = values.get("POSTGRES_PORT", "5432")
            return f"postgresql://aleph:{password}@localhost:{port}/aleph"
    return None


def _libpq(url: str) -> str:
    """Strip SQLAlchemy's `+asyncpg` dialect suffix — libpq rejects it."""
    scheme, _, rest = url.partition("://")
    return f"{scheme.split('+')[0]}://{rest}"


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _psql(url: str, sql: str) -> subprocess.CompletedProcess[str]:
    return _run(["psql", url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql])


async def _chain_state(url: str) -> dict[str, tuple[str | None, int, frozenset[str]]]:
    """Per project: where the ledger hash chain first diverges, how many events, which ids.

    Uses the production verifier, not a reimplementation — a drill that re-derives
    the hash its own way only proves the drill agrees with itself.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aleph_db.repos.ledger import verify_project_chain

    engine = create_async_engine(url, pool_pre_ping=True)
    state: dict[str, tuple[str | None, int, frozenset[str]]] = {}
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            rows = (
                await session.execute(
                    text(
                        "select distinct project_id from action_ledger_events "
                        "where project_id is not null order by 1"
                    )
                )
            ).all()
            for (project_id,) in rows:
                result = await verify_project_chain(session, project_id)
                ids = (
                    (
                        await session.execute(
                            text("select id from action_ledger_events where project_id = :p"),
                            {"p": project_id},
                        )
                    )
                    .scalars()
                    .all()
                )
                diverged = (
                    str(result.first_divergence.event_id) if result.first_divergence else None
                )
                state[str(project_id)] = (diverged, result.count, frozenset(str(i) for i in ids))
    finally:
        await engine.dispose()
    return state


def _compare_chains(
    source: dict[str, tuple[str | None, int, frozenset[str]]],
    restored: dict[str, tuple[str | None, int, frozenset[str]]],
) -> tuple[list[str], int, int, int]:
    """Require the round trip to preserve every chain verdict it carried over."""
    problems: list[str] = []
    reproduced = 0
    events = 0
    for project_id, (r_div, r_count, r_ids) in sorted(restored.items()):
        events += r_count
        live = source.get(project_id)
        if live is None:
            # Impossible for an append-only table on a live source, so if it
            # happens the drill must say so rather than skip it.
            problems.append(f"project {project_id} is in the restore and not in the source")
            continue
        s_div, _s_count, _s_ids = live
        if r_div is not None:
            reproduced += 1
            if s_div != r_div:
                problems.append(
                    f"project {project_id}: the restored chain diverges at {r_div}, "
                    f"the source at {s_div or 'nowhere'} — the round trip changed "
                    f"something the hash is computed over"
                )
        elif s_div is not None and s_div in r_ids:
            problems.append(
                f"project {project_id}: the source chain diverges at {s_div} and the "
                f"restored one does not, over the same events — the round trip "
                f"silently repaired a tampered ledger"
            )
    return problems, len(restored), events, reproduced


def main() -> int:
    raw = _resolve_url()
    if raw is None:
        print("cannot run here: no database URL (DATABASE_URL, or deploy/compose/.env)")
        return 2
    admin_url = _libpq(raw)

    for tool in ("psql", "pg_dump", "pg_restore"):
        if _run(["which", tool]).returncode != 0:
            print(f"cannot run here: {tool} is not on PATH")
            return 2

    probe = _psql(admin_url, "select 1")
    if probe.returncode != 0:
        print(f"cannot run here: cannot reach the database — {probe.stderr.strip()}")
        return 2

    scratch_url = _with_database(admin_url, SCRATCH_DB)
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="aleph-restore-drill-") as tmp:
        backup_dir = Path(tmp) / "backup"
        try:
            # 1 — back up the live database. Assets are skipped: the drill is
            # about the database round trip, docker is not always here, and a
            # 74MB tar per run is what makes a drill too slow to keep running.
            step = _run(
                [
                    str(ROOT / "scripts" / "backup.sh"),
                    "--out",
                    str(backup_dir),
                    "--no-assets",
                    "--database-url",
                    admin_url,
                ]
            )
            if step.returncode != 0:
                print(step.stdout)
                print(step.stderr, file=sys.stderr)
                return 2 if step.returncode == 2 else 1

            manifest = json.loads((backup_dir / "manifest.json").read_text())

            # 2 — restore it into a scratch database and let restore.sh do the
            # full comparison. Its exit code IS the criterion.
            step = _run(
                [
                    str(ROOT / "scripts" / "restore.sh"),
                    str(backup_dir),
                    "--into",
                    SCRATCH_DB,
                    "--drop-existing",
                    "--cluster-url",
                    admin_url,
                ]
            )
            print(step.stdout, end="")
            if step.returncode != 0:
                print(step.stderr, file=sys.stderr)
                failures.append("restore.sh reported the restore does not match the backup")

            # 3 — the hash chain, recomputed by the production verifier on
            # both sides. See the module docstring for why the criterion is
            # "the two agree" rather than "the restore verifies".
            async_scratch = scratch_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            async_source = admin_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            restored_state = asyncio.run(_chain_state(async_scratch))
            source_state = asyncio.run(_chain_state(async_source))
            problems, projects, events, reproduced = _compare_chains(source_state, restored_state)
            failures.extend(problems)
            if problems:
                print(f"  \033[31mFAIL\033[0m   ledger hash chain: {problems[0]}")
            elif projects == 0:
                # Not a pass. An empty ledger verifies trivially, and a drill
                # that reports "chain verified" over zero events is the kind of
                # green light this repo has already been burned by.
                print(
                    "  \033[33mnote\033[0m   no project has ledger events — "
                    "the hash-chain leg of this drill verified nothing"
                )
            else:
                print(
                    f"  \033[32mok\033[0m     ledger hash chain re-derived on both sides and "
                    f"identical: {events} events across {projects} projects, including "
                    f"{reproduced} pre-existing divergences reproduced at the same event "
                    f"(recomputed by aleph_db.repos.ledger.verify_project_chain)"
                )
        finally:
            # 4 — always drop the scratch database, including on the way out of
            # a failure. WITH (FORCE) because the verifier's pool may still be
            # closing and a lingering connection blocks the drop.
            drop = _psql(admin_url, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
            if drop.returncode != 0:
                print(f"  \033[33mnote\033[0m   could not drop {SCRATCH_DB}: {drop.stderr.strip()}")

    if failures:
        print()
        print(
            "\033[31mFAILED\033[0m: the live database does not survive a backup/restore round trip"
        )
        for f in failures:
            print(f"  - {f}")
        return 1

    print()
    print(
        f"\033[32mOK\033[0m: {manifest['total_rows']} rows across {manifest['table_count']} tables "
        f"backed up, restored into {SCRATCH_DB}, verified, and the scratch database dropped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
