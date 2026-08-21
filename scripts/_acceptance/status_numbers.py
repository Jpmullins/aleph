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
import sys

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
        "citations with no verbatim quote or no character span (number 3)",
    ),
    (
        "uncosted_model_calls",
        "select count(*) from model_calls where pricing_source = 'unknown' or agent_run_id is null",
        "== 0",
        "model calls with no price or no run to attribute them to (number 5)",
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
    raise AssertionError(predicate)


async def main() -> int:
    url = os.environ.get("DATABASE_URL") or os.environ.get("ALEPH_DATABASE_URL")
    if not url:
        for key, _sql, _pred, note in QUERIES:
            print(f"{key}\tn/a\tunknown\tno DATABASE_URL — {note}")
        return 0

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            for key, sql, predicate, note in QUERIES:
                try:
                    value = (await conn.execute(text(sql))).scalar_one()
                except Exception as exc:  # an unreachable DB is a status, not a crash
                    print(f"{key}\tn/a\tunknown\t{type(exc).__name__} — {note}")
                    continue
                print(f"{key}\t{value}\t{_verdict(int(value), predicate)}\t{note}")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
