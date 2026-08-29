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

#: What makes a project REAL rather than test litter.
#:
#: Defined once, and used both by the numbers that exclude fixtures and by the
#: guard that asks whether anything is left. Those two have to mean exactly the
#: same thing: a guard counting real projects by a different rule than the
#: numbers exclude them by would report "there are real projects to measure"
#: while every number's own predicate matched none of them — the vacuous green
#: with an extra step between it and the reader.
_NOT_A_FIXTURE = "p.title not ilike '[e2e]%' and p.title not ilike 'smoke test%'"


def _in_a_real_project(fk: str, *, negate: bool = False) -> str:
    """`AND [NOT] EXISTS (a non-fixture project owning this row)`."""
    return (
        f"   and {'not ' if negate else ''}exists ("
        f"     select 1 from projects p where p.id = {fk} and {_NOT_A_FIXTURE}"
        f"   )"
    )


#: How many projects the numbers above would actually look at.
REAL_PROJECTS_SQL = f"select count(*) from projects p where {_NOT_A_FIXTURE}"

#: Numbers that count defects "in real projects". Every one of them reads 0 when
#: there are no real projects at all, and 0 is this scoreboard's word for "no
#: defects". Guarded in `main` so that case prints `n/a` instead.
REAL_PROJECT_SCOPED = frozenset(
    {
        "unindexed_documents",
        "ungrounded_citations",
        "sourceless_citations",
        "degraded_indexes",
    }
)

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
        # Scoped to REAL projects, like `ungrounded_citations` below and for the
        # same reason. Every one of the 49 this counted on 2026-08-22 was in an
        # `[e2e]` project created and soft-deleted by the browser suite: the
        # number was reporting on test litter and moved whenever the suite ran.
        # A health number a test run can turn red is a number people learn to
        # ignore.
        #
        # The excluded count is printed beside it as `unindexed_documents_
        # fixtures`, so narrowing the scope cannot hide anything behind it.
        "select count(*) from normalized_documents nd"
        " where not exists ("
        "   select 1 from document_chunks c where c.normalized_document_id = nd.id)"
        + _in_a_real_project("nd.project_id"),
        "== 0",
        "ingested documents with no chunks at all, in real projects",
    ),
    (
        "unindexed_documents_fixtures",
        "select count(*) from normalized_documents nd"
        " where not exists ("
        "   select 1 from document_chunks c where c.normalized_document_id = nd.id)"
        + _in_a_real_project("nd.project_id", negate=True),
        ">= 0",
        "the same count in test-fixture and orphaned projects — excluded above, "
        "printed here so the scope cannot hide anything",
    ),
    (
        "ungrounded_citations",
        # Scoped to REAL projects, and the exclusion is reported beside it.
        #
        # This number asks how much of what Aleph believes is unevidenced. A
        # test fixture's beliefs are not part of that, and the e2e suites write
        # through the legacy path into projects they create — which is how this
        # went from 796 to 3,997 in one afternoon of running tests, none of it
        # about the corpus.
        #
        # The rows cannot simply be cleaned up: citations hang off claims, which
        # hang off pages, which are held by a foreign key from `wiki_revisions`
        # — append-only by database trigger. So test-created beliefs outlive
        # their project permanently, exactly as test-created pages do.
        #
        # `ilike`, not `like`. The exclusion was case-sensitive and the
        # instance's own smoke corpus is titled "Smoke test" — lowercase t —
        # while the pattern read 'Smoke Test%'. So "Smoke Test 2" and "Smoke
        # Test 3" were excluded as fixtures and "Smoke test" was counted as a
        # real project, on nothing but the case of one letter. It carried 220
        # of the 226 this number reported.
        #
        # Matched on the title convention the fixtures actually use, and
        # `ungrounded_citations_fixtures` prints what was excluded, so this can
        # narrow the number and cannot hide anything behind it.
        "select count(*) from citations c"
        " where (c.quote is null or c.char_start is null)"
        "   and c.source_id is not null" + _in_a_real_project("c.project_id"),
        "== 0",
        "citations with a source but no quote or span, in real projects — "
        "RE-DERIVABLE by re-reading the source (decisions.md D9)",
    ),
    (
        "sourceless_citations",
        # Counted APART, and still graded. A citation with `source_id IS NULL`
        # is not an unevidenced belief — it is a broken row, and the two need
        # opposite fixes: an unquoted citation is re-derivable by re-reading its
        # source, and one with no source has nothing to re-read. Folding them
        # together made "run BeliefService.rebuild" the stated remedy for six
        # rows it cannot touch.
        "select count(*) from citations c"
        " where c.source_id is null" + _in_a_real_project("c.project_id"),
        "== 0",
        "citations naming no source at all, in real projects — NOT re-derivable; "
        "the write path that produced them is the defect",
    ),
    (
        "ungrounded_citations_fixtures",
        "select count(*) from citations c"
        " where (c.quote is null or c.char_start is null)"
        + _in_a_real_project("c.project_id", negate=True),
        ">= 0",
        "the same count in test-fixture and orphaned projects — excluded above, "
        "printed here so the scope cannot hide anything",
    ),
    (
        "uncosted_model_calls",
        # Measured FORWARD. `docs/decisions.md` D9: the historical rows are true
        # records of calls made while the agent path had no price list and no
        # run to attribute to. Deleting or backfilling them would make the
        # append-only ledger say something nobody knows to be true, so the
        # measurement moves rather than the data.
        #
        # TWO defects, not one, and they are not the same defect.
        #
        # An UNPRICED call is always wrong: it records $0 for money that was
        # spent, so a broken pricing table reads as a cheap day. That applies to
        # every call, whoever made it.
        #
        # An UNATTRIBUTED call is only wrong on the AGENT path. `assistant.*`
        # runs inside a turn and there is always a run to name; the diagnostic
        # smoke route and the non-agent chat route are HTTP requests, and there
        # is no agent run for them to belong to. Requiring one there would mean
        # minting a fake run so a number could be zero, which is the shape of
        # thing this file exists to stop.
        #
        # So: unpriced anywhere, or unattributed on a purpose that names the
        # agent. The prefix is the honest discriminator — `purpose` is what the
        # call site declares itself to be.
        "select count(*) from model_calls where timestamp >= :cutoff and ("
        "  pricing_source = 'unknown'"
        "  or (agent_run_id is null and purpose like 'assistant%')"
        ")",
        "== 0",
        "model calls since the cutoff: unpriced anywhere, or unattributed on the "
        "agent path (number 5, measured forward)",
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
        # Scoped to REAL projects, like the two numbers above. All five this
        # counted on 2026-08-22 were in one `[e2e] chat project` — a browser-suite
        # fixture, soft-deleted, whose sources were never going to be embedded.
        "select count(*) from retrieval_index_records r"
        " where r.state <> 'embedded'" + _in_a_real_project("r.project_id"),
        "== 0",
        "sources searchable by keyword only, in real projects",
    ),
    (
        "degraded_indexes_fixtures",
        "select count(*) from retrieval_index_records r"
        " where r.state <> 'embedded'" + _in_a_real_project("r.project_id", negate=True),
        ">= 0",
        "the same count in test-fixture and orphaned projects — excluded above, "
        "printed here so the scope cannot hide anything",
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
            # How many projects the fixture-scoped numbers can actually see.
            #
            # Each of them counts defects "in real projects", and each reads 0
            # when there are none — which this scoreboard renders as `ok`. That
            # is the failure the module docstring names, reached from the other
            # direction: not a number nobody computed, but a number computed
            # correctly over an empty set. It happened here. A database cleanup
            # left one project titled "Smoke Test 2", the fixture patterns
            # excluded it, and `ungrounded citations 0 ok` sat on the dashboard
            # beside `...in test fixtures 544` — the whole corpus, reclassified
            # as litter by its own title, with the green above it unchanged.
            try:
                real_projects = int((await conn.execute(text(REAL_PROJECTS_SQL))).scalar_one())
            except Exception:  # the per-number handler below reports it properly
                real_projects = -1

            for key, sql, predicate, note in QUERIES:
                if key in REAL_PROJECT_SCOPED and real_projects == 0:
                    print(
                        f"{key}\tn/a\tunknown\tno project on this instance is anything but a "
                        f"test fixture, so this cannot fail — {note}"
                    )
                    continue
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
