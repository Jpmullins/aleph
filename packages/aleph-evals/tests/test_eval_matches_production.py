"""The eval has to measure the system that ships, not a friendlier one.

Two mismatches, both invisible because each side looked correct on its own:

- The eval embedded `f"{title}. {text}"`; the ingest path embedded the raw
  chunk. Every retrieval number ever reported was measured against a corpus
  with a hint prepended that production never adds.
- The eval seeded ONE chunk per document with `section_path=None` and
  `char_start=0`, then disabled the per-source cap because "one chunk per
  source anyway". So chunking, overlap, section paths, and the cap that stops a
  long document filling every slot were all outside the measurement.

Neither would ever fail a test that only asked "does retrieval work". They are
the reason `WS-RS6` and `WS-RS10` would otherwise be unfalsifiable: a change
measured on a better-conditioned corpus reports an improvement the running
system does not have.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from aleph_evals import retrieval_eval
from aleph_rks import indexing
from aleph_rks.indexing import embedding_text

EVAL_SRC = pathlib.Path(retrieval_eval.__file__)
# Resolved through the imported module, not by walking up from this file. A
# relative path here breaks the moment a package moves, and breaks as a
# FileNotFoundError rather than as a clear failure.
INDEXING_SRC = pathlib.Path(indexing.__file__)


def _calls(path: pathlib.Path, name: str) -> int:
    """Count CALLS to `name`, not mentions.

    An AST walk rather than a grep, because a grep counts the import line, the
    docstring that explains the function, and the comment warning you not to
    change it — three hits for zero call sites.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    )


def test_eval_and_production_embed_the_same_text() -> None:
    """One function, called on both sides. Not two implementations that agree."""
    assert _calls(EVAL_SRC, "embedding_text") >= 1, "the eval builds its own embed string"
    assert _calls(INDEXING_SRC, "embedding_text") >= 1, "the ingest path builds its own"


def test_the_shared_function_returns_the_chunk_unchanged() -> None:
    """And it does NOT prepend the title, deliberately.

    Prefixing is a real technique and may be worth adopting — but not as a side
    effect of reconciling an eval. Thousands of chunks are already embedded
    without it, and changing this alone would leave the index holding two
    representations of one corpus, with new documents ranking against old ones
    on an unequal footing. Adopting it means a deliberate re-embed (WS-RS4).
    """
    assert embedding_text(chunk_text="a passage") == "a passage"
    assert embedding_text(chunk_text="a passage", title="A Title") == "a passage"


def test_the_eval_runs_the_real_chunker() -> None:
    """Chunking is inside the measurement — WS-RS5 criterion 4."""
    assert _calls(EVAL_SRC, "chunk_markdown") >= 1


def test_the_eval_applies_the_per_source_cap() -> None:
    """`per_source_cap=None` disabled the very thing it should be measuring.

    Justified once by one-chunk-per-document seeding. That is no longer true,
    and a measurement that switches off a production behaviour cannot see it
    working — or failing.
    """
    source = EVAL_SRC.read_text()
    assert "per_source_cap=None" not in source
    assert "per_source_cap=PER_SOURCE_CAP" in source


def test_the_report_carries_ranking_metrics_not_only_recall() -> None:
    """Recall cannot tell "first" from "eighth".

    So a reranker that reorders the same eight results — exactly what WS-RS6
    proposes — is worth precisely zero to it, and would be measured as no
    improvement whatever it did.
    """
    report = retrieval_eval.Report(
        mode="hybrid",
        k=8,
        total=2,
        hits=1,
        by_phrasing={},
        misses=[],
        ndcg=0.5,
        mrr=0.75,
        recall_at={1: 0.5, 3: 1.0},
    )
    rendered = report.render()
    assert "nDCG@10" in rendered
    assert "MRR" in rendered
    assert "@1 0.50" in rendered


def test_an_absent_abstain_metric_says_so_rather_than_reporting_zero() -> None:
    """A metric silently missing reads as a metric that passed."""
    report = retrieval_eval.Report(mode="hybrid", k=8, total=1, hits=1, by_phrasing={}, misses=[])
    assert "abstain   n/a" in report.render()
    assert retrieval_eval.UNANSWERABLE in report.render()


def test_ndcg_rewards_rank_and_not_merely_presence() -> None:
    """The property that makes nDCG worth computing at all."""
    ndcg = retrieval_eval._ndcg_at
    wanted = {"a"}
    first = ndcg(["a", "b", "c"], wanted, 10)
    last = ndcg(["b", "c", "a"], wanted, 10)
    assert first == 1.0
    assert 0.0 < last < first
    assert ndcg(["b", "c", "d"], wanted, 10) == 0.0
    # Beyond the cut-off is a miss, not a small win — otherwise the cut-off is
    # decorative.
    assert ndcg(["x"] * 10 + ["a"], wanted, 10) == 0.0


def test_ndcg_is_one_when_every_wanted_source_leads() -> None:
    """The ideal denominator has to account for MULTI-source answers.

    With `ideal` computed for a single hit, a question with two correct sources
    both ranked first and second scores above 1.0 — a number that cannot be
    compared to anything.
    """
    assert retrieval_eval._ndcg_at(["a", "b", "c"], {"a", "b"}, 10) == 1.0


def test_inspect_finds_the_signature_the_eval_relies_on() -> None:
    """`embedding_text` is keyword-only; a positional call would be a silent
    swap of chunk text and title."""
    params = inspect.signature(embedding_text).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_the_report_prints_the_size_of_the_set_it_measured() -> None:
    """WS-RS5 c1's second clause: the floor is on a number nobody could see.

    The criterion is ">= 300 documents and >= 150 questions ... and the eval
    run against it reports its size". Every metric was printed and the size was
    not, so a saturated 12-document toy and a 740-document generated set
    rendered identically apart from the numbers — and the difference between
    "retrieval got better" and "the ruler got shorter" was invisible.
    """
    report = retrieval_eval.Report(
        mode="hybrid",
        k=8,
        total=200,
        hits=160,
        by_phrasing={},
        misses=[],
        corpus_documents=740,
        corpus_chunks=4245,
        corpus_questions=208,
        dataset_dir="/tmp/generated-set",
    )
    rendered = report.render()
    assert "740 documents" in rendered
    assert "4245 chunks" in rendered
    assert "208 questions" in rendered
    assert "/tmp/generated-set" in rendered


def test_the_claim_arm_does_not_report_a_chunk_count_of_zero() -> None:
    """A zero beside a real number reads as an empty index.

    The claim arm seeds no chunks at all and prints its own `seeded N claims`
    line. `0 chunks` there would be read as "the corpus is empty", which is the
    same confusion between an absent measurement and a measured absence that
    `abstain n/a` exists to avoid.
    """
    report = retrieval_eval.Report(
        mode="hybrid",
        k=8,
        total=10,
        hits=2,
        by_phrasing={},
        misses=[],
        surface=retrieval_eval.CLAIMS,
        corpus_documents=740,
        corpus_chunks=0,
        corpus_questions=208,
    )
    rendered = report.render()
    assert "740 documents" in rendered
    assert "chunks" not in rendered


def test_the_reported_size_is_measured_and_cannot_be_a_literal() -> None:
    """The field existing is not the field being filled.

    A `corpus_documents: int = 0` that `run()` never binds renders `0 documents`
    for every set ever measured, which is worse than printing nothing: it is a
    number, so it reads as having been counted. This asserts the `Report(...)`
    that `run()` actually constructs binds each size keyword to an EXPRESSION —
    `len(corpus)`, not `740` and not `0`.
    """
    tree = ast.parse(EVAL_SRC.read_text())
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    )
    constructed = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Report"
    ]
    assert len(constructed) == 1, "run() should build exactly one Report"
    bound = {kw.arg: kw.value for kw in constructed[0].keywords}
    for field_name in ("corpus_documents", "corpus_chunks", "corpus_questions"):
        assert field_name in bound, f"run() does not report {field_name}"
        assert not isinstance(bound[field_name], ast.Constant), (
            f"{field_name} is a literal — it would report the same size for every set"
        )
