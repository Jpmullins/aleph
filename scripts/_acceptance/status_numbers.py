"""The database half of the eight numbers that mean done.

Printed as `key<TAB>value<TAB>verdict<TAB>note` so `scripts/status.sh` can lay it
out beside the numbers that come from running commands.

Every query here is the exact query `docs/plan.md` Part 1 names. Where a number
cannot be computed yet, this prints `n/a` and says what is missing — never a
zero, because a zero that means "nothing measured" and a zero that means "no
defects" look identical and one of them is a lie.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

#: `key`, the SQL, the predicate for "good", and what it means in words.
QUERIES: list[tuple[str, str, str, str]] = [
    (
        "chunks",
        "select count(*) from document_chunks",
        "> 0",
        "searchable passages in the production index",
    ),
    (
        "chunks_embedded",
        "select count(*) from document_chunks where embedding is not null",
        "> 0",
        "passages the dense leg can see",
    ),
    (
        "unindexed_documents",
        "select count(*) from normalized_documents nd where not exists "
        "(select 1 from document_chunks c where c.normalized_document_id = nd.id)",
        "== 0",
        "ingested documents with no chunks at all",
    ),
    (
        "ungrounded_citations",
        "select count(*) from citations where quote is null or char_start is null",
        "== 0",
        "citations with no quote or span — RE-DERIVABLE, so this stays red until "
        "WS-RS8 runs the extractor (decisions.md D9)",
    ),
    (
        "uncosted_model_calls",
        # Measured FORWARD. `docs/decisions.md` D9: the historical rows are true
        # records of calls made while the agent path had no price list and no
        # run to attribute to. Deleting or backfilling them would make the
        # append-only ledger say something nobody knows to be true, so the
        # measurement moves rather than the data.
        "select count(*) from model_calls where (pricing_source = 'unknown' "
        "or agent_run_id is null) and timestamp >= :cutoff",
        "== 0",
        "model calls since the cutoff with no price or no run (number 5, measured forward)",
    ),
    (
        "uncosted_model_calls_legacy",
        "select count(*) from model_calls where pricing_source = 'unknown' or agent_run_id is null",
        ">= 0",
        "the same count over ALL time — retained, never edited (decisions.md D9)",
    ),
    (
        "stuck_agent_runs",
        "select count(*) from agent_runs where status = 'running' "
        "and started_at < now() - interval '1 hour'",
        "== 0",
        "runs whose owning process died without saying so (number 6)",
    ),
    (
        "degraded_indexes",
        "select count(*) from retrieval_index_records where state <> 'embedded'",
        "== 0",
        "sources searchable by keyword only",
    ),
]


def _verdict(value: int, predicate: str) -> str:
    if predicate == "== 0":
        return "ok" if value == 0 else "FAIL"
    if predicate == "> 0":
        return "ok" if value > 0 else "FAIL"
    if predicate == ">= 0":
        # Reported, never graded. A number printed for honesty rather than as a
        # target — see `uncosted_model_calls_legacy`.
        return "info"
    raise AssertionError(predicate)


#: When the deployed stack started recording model calls with a price and a run.
#: Read from a file rather than hardcoded so the value has a place to explain
#: itself, and so moving it is a visible edit rather than a constant nobody sees.
CUTOFF_FILE = pathlib.Path(__file__).resolve().parents[2] / "docs" / "attribution-cutoff.txt"


def _cutoff() -> datetime | None:
    try:
        first = CUTOFF_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
        return datetime.fromisoformat(first.replace("Z", "+00:00"))
    except (OSError, ValueError, IndexError):
        return None


async def main() -> int:
    url = os.environ.get("DATABASE_URL") or os.environ.get("ALEPH_DATABASE_URL")
    if not url:
        for key, _sql, _pred, note in QUERIES:
            print(f"{key}\tn/a\tunknown\tno DATABASE_URL — {note}")
        return 0

    cutoff = _cutoff()
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            for key, sql, predicate, note in QUERIES:
                if ":cutoff" in sql:
                    # A cutoff in the FUTURE makes "zero since the cutoff"
                    # arithmetically true and completely uninformative. Refuse
                    # to report it as good — an unfalsifiable green is the exact
                    # failure this whole scoreboard exists to remove.
                    if cutoff is None:
                        print(f"{key}\tn/a\tunknown\tno cutoff recorded in {CUTOFF_FILE.name}")
                        continue
                    if cutoff > datetime.now(UTC):
                        print(
                            f"{key}\tn/a\tunknown\tthe cutoff {cutoff.isoformat()} is in the "
                            f"future, so this cannot fail — {note}"
                        )
                        continue
                try:
                    value = (
                        await conn.execute(text(sql), {"cutoff": cutoff} if cutoff else {})
                    ).scalar_one()
                except Exception as exc:  # an unreachable DB is a status, not a crash
                    print(f"{key}\tn/a\tunknown\t{type(exc).__name__} — {note}")
                    continue
                print(f"{key}\t{value}\t{_verdict(int(value), predicate)}\t{note}")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
