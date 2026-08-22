"""`python -m aleph_connectors.reencrypt` — step three of a key rotation.

The whole rotation, and the order is not negotiable:

1. Install the new key. Set ``ALEPH_CREDENTIAL_LEGACY_KEY`` to the key currently
   in use (or leave it unset, in which case the pre-split
   ``ALEPH_AGENT_TOKEN_SECRET`` is assumed, which is what v1 rows were keyed
   by), set ``ALEPH_CREDENTIAL_MASTER_KEY`` to the new one, and restart. Both
   generations are now readable; new writes go to the new key.
2. Verify: ``--dry-run`` reports how many rows are still on the old key and
   whether every one of them can be opened. A row it cannot open is a
   credential that is *already* lost, and finding that out before the old key
   goes away is the entire point of doing this in steps.
3. Re-encrypt: run without ``--dry-run``. Each row is re-encrypted and ledgered
   individually, so a bad row is reported and skipped rather than aborting the
   pass.
4. Only when a dry run reports zero rows remaining, remove
   ``ALEPH_CREDENTIAL_LEGACY_KEY`` and restart.

Doing 1 and 4 together is the failure mode: there is no moment at which both
keys are readable, so any row not re-encrypted in the gap is gone.

Exit codes: 0 = nothing left to do, or everything moved. 1 = at least one row
could not be opened. 2 = misconfiguration (no database URL, no master key).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aleph_connectors.credentials import (
    ConnectorCredentialService,
    ReencryptReport,
    credential_cipher,
)
from aleph_connectors.keys import master_key_from_env
from aleph_core.errors import AlephError
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal

#: The actor a rotation is attributed to. `UUID(int=0)` is what
#: `aleph_db.repos.agent_runs.SYSTEM_ACTOR` already uses for machine-initiated
#: events; reusing it keeps one system identity in the ledger rather than two.
SYSTEM_ACTOR = UUID(int=0)


def _operator_principal() -> Principal:
    return Principal(
        user_id=SYSTEM_ACTOR,
        subject="credential-reencrypt",
        email="operator@aleph.local",
        actor_kind="system",
    )


async def reencrypt_credentials(
    *,
    database_url: str,
    master_key: str,
    legacy_key: str,
    dry_run: bool = False,
    project_id: UUID | None = None,
) -> ReencryptReport:
    """Re-encrypt every credential not already on the current key.

    Importable, so the rotation procedure documented in `docs/operations.md` is
    the same code the rotation test runs end to end rather than a shell snippet
    nobody executes.
    """
    cipher = credential_cipher(master_key=master_key, legacy_key=legacy_key)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            svc = ConnectorCredentialService(session, cipher=cipher)
            report = await svc.reencrypt(
                ledger=LedgerWriter(session),
                principal=_operator_principal(),
                project_id=project_id,
            )
            if dry_run:
                # The pass already decrypted every row to find out whether it
                # COULD be opened — that is the check worth having — and then
                # throws the writes away.
                await session.rollback()
                report.reencrypted = 0
            else:
                await session.commit()
            return report
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aleph_connectors.reencrypt",
        description="Re-encrypt connector credentials onto the current master key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would move, and whether every row can be opened, without writing.",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Limit to one project (default: every project in the deployment).",
    )
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    master_key, legacy_key = master_key_from_env()
    if not master_key:
        print("ALEPH_CREDENTIAL_MASTER_KEY is not set", file=sys.stderr)
        return 2

    try:
        report = asyncio.run(
            reencrypt_credentials(
                database_url=database_url,
                master_key=master_key,
                legacy_key=legacy_key,
                dry_run=args.dry_run,
                project_id=UUID(args.project_id) if args.project_id else None,
            )
        )
    except AlephError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    verb = "would re-encrypt" if args.dry_run else "re-encrypted"
    moved = report.examined if args.dry_run else report.reencrypted
    print(
        f"target key_version={report.target_version} "
        f"rows on an older key={report.examined} {verb}={moved - len(report.failures)}"
    )
    for cred_id, reason in report.failures:
        print(f"  UNREADABLE {cred_id}: {reason}", file=sys.stderr)
    if report.failures:
        print(
            f"{len(report.failures)} credential(s) could not be opened with any configured key. "
            f"Do NOT remove ALEPH_CREDENTIAL_LEGACY_KEY.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
