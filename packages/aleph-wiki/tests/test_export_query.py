"""The ORDER BY the vault export's determinism rests on (WS-H8).

`tests/integration/test_vault_evidence_export.py` pins six of the seven columns
*behaviourally*: it stores a corpus back to front, so the physical order
Postgres would return is demonstrably the wrong answer and the query has to
sort. That is the strong form and it is where the real coverage is.

This file exists for the seventh, and for an honest statement of what a test
can and cannot reach.

**Why the behavioural pin cannot cover `WikiClaim.id`.** The planner reaches
`wiki_claims` through `ix_claims_live`, so two claims tying on `text` already
arrive at the sort node in id order. Dropping the id tiebreaker therefore
changes nothing observable — measured, not assumed: deleting that one line and
running the integration file gives `13 passed`, while deleting any of the other
six turns it red. Making it fail behaviourally would need the rows to reach the
sort in NON-id order, which means controlling the plan (a forced sequential
scan with `enable_indexscan = off`, or enough rows that the planner chooses one)
— a test that asserts a planner decision, not a format property, and one that
would go green again the day the planner changed its mind for an unrelated
reason.

So the seventh is pinned on the statement instead. That is a weaker kind of
check and it is worth being precise about why it is still worth having: it
cannot prove the database honours the clause, but it does prove the clause is
still being asked for, which is exactly the mutation that was silent. Deleting
the whole seven-column ORDER BY left thirteen integration tests green.
"""

from __future__ import annotations

from uuid import UUID

from aleph_wiki.export_service import EVIDENCE_ORDER_BY, evidence_query

PROJECT_ID = UUID("01a02790-c0e6-7eea-a5f1-ca3c87b89f0d")


def _order_by_columns() -> list[str]:
    """The compiled statement's ORDER BY, as a list of column names."""
    compiled = str(evidence_query(PROJECT_ID))
    assert "ORDER BY" in compiled, "the export query no longer orders its rows at all"
    clause = compiled.split("ORDER BY", 1)[1]
    return [part.strip() for part in clause.strip().splitlines()[0].split(",")]


def test_the_export_query_asks_for_every_tiebreaker_in_order() -> None:
    """Seven columns, in this sequence, and no fewer.

    Order matters as much as membership: sorting citations by span before
    marker groups the bundle differently, which is a different set of bytes
    from the same rows and would defeat the byte-identity criterion for a
    reason no diff would explain.
    """
    assert _order_by_columns() == list(EVIDENCE_ORDER_BY)


def test_the_declared_ordering_names_the_columns_the_format_depends_on() -> None:
    """`EVIDENCE_ORDER_BY` is prose until something compares it to the query.

    A constant that documents a clause and is never checked against it is the
    same shape as the header fields this workstream found written and never
    read — it agrees with whatever it is put next to.
    """
    assert EVIDENCE_ORDER_BY == (
        "wiki_pages.slug",
        "wiki_claims.text",
        "wiki_claims.id",
        "citations.citation_marker",
        "citations.source_id",
        "citations.char_start",
        "citations.id",
    )


def test_the_query_still_filters_superseded_and_stub_rows() -> None:
    """Extracting the statement out of `load_page_evidence` must not have left
    the WHERE clause behind — the integration tests cover what it selects, this
    covers that the extraction moved all of it."""
    compiled = str(evidence_query(PROJECT_ID))
    assert "wiki_claims.superseded_by IS NULL" in compiled
    assert "wiki_pages.is_stub IS false" in compiled
    assert "LEFT OUTER JOIN citations" in compiled
    assert "LEFT OUTER JOIN sources" in compiled
