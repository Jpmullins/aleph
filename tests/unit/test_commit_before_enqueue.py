"""E3.4 — a worker must never be handed the id of an uncommitted row.

Every background job in Aleph is dispatched the same way: the route writes rows,
mints an agent token, and enqueues a job carrying the new ids. If the enqueue
happens inside the still-open transaction, the worker and the commit race — and
an arq worker on a warm Redis wins that race often enough to matter.

The loser is invisible. `normalize_job` looked the `SourceVersion` up, found
nothing, and raised a plain exception. **arq treats a plain exception as
terminal**: no retry, no redelivery. The source sat in `ingested` forever with
no failure recorded anywhere a user could see, which reads as "still
processing" rather than "broken".

Six routes enqueued before committing. `sources.py:_kick_off_normalize` was the
sharpest instance — its own docstring said "the row is committed by the caller",
which was true, and happened *after* the job had already been published.

Two guards, because either alone is weak:

* a **static** one, so a new route cannot reintroduce the ordering; and
* a **behavioural** one over the real `_missing_row` policy, because "commit
  first" is a mitigation and the job still has to survive losing the race.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTES = REPO_ROOT / "apps" / "api" / "src" / "aleph_api" / "routes"

#: Routes that enqueue but write nothing the worker reads back. Listed
#: explicitly, with the reason, so the exemption is a decision rather than a
#: hole — a route added here without a reason is a review failure.
_NO_ROW_TO_RACE = {
    # Writes only a ledger event; `editorial_review_job` creates the ReviewRun
    # itself and reads nothing this route wrote.
    ("reviews.py", "start_editorial_review"),
}


def _enqueue_sites() -> list[tuple[str, str, bool]]:
    """`(file, function, commits_before_first_enqueue)` for every dispatcher."""
    out: list[tuple[str, str, bool]] = []
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            calls = sorted(
                (n.lineno, getattr(n.func, "attr", ""))
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") in ("commit", "enqueue_job")
            )
            if not any(kind == "enqueue_job" for _, kind in calls):
                continue
            first = next(line for line, kind in calls if kind == "enqueue_job")
            commits = any(kind == "commit" and line < first for line, kind in calls)
            out.append((path.name, fn.name, commits))
    return out


def test_dispatchers_were_actually_found() -> None:
    """Guard the guard: an empty sweep would pass vacuously forever."""
    sites = _enqueue_sites()
    assert len(sites) >= 8, f"only found {len(sites)} enqueue sites; the AST walk is not working"


@pytest.mark.parametrize(
    ("filename", "function", "commits"),
    _enqueue_sites(),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_route_commits_before_it_enqueues(filename: str, function: str, commits: bool) -> None:
    if (filename, function) in _NO_ROW_TO_RACE:
        pytest.skip("exempt: writes nothing the dispatched worker reads back")
    assert commits, (
        f"{filename}:{function} enqueues a job before committing. The worker "
        f"resolves the ids it is handed the moment it dequeues, so it can look "
        f"for rows this transaction has not written yet — and the resulting "
        f"failure is terminal in arq, leaving the record stuck with no error "
        f"surfaced."
    )


class TestMissingRowIsRetryableThenFatal:
    """Committing first is a mitigation; losing the race must still be survivable."""

    def test_first_attempts_ask_arq_to_retry(self) -> None:
        from arq.worker import Retry

        from aleph_workers.jobs.normalize import _missing_row

        for attempt in (1, 2, 3):
            exc = _missing_row({"job_try": attempt}, "source version X not found")
            assert isinstance(exc, Retry), (
                f"attempt {attempt} produced {type(exc).__name__}, which arq "
                f"treats as terminal — a row that is merely not visible yet "
                f"would never be retried"
            )

    def test_retries_back_off(self) -> None:
        from aleph_workers.jobs.normalize import _missing_row

        # arq stores the deferral as `defer_score` (milliseconds), not `defer`.
        defers = [
            _missing_row({"job_try": n}, "x").defer_score  # type: ignore[attr-defined]
            for n in (1, 2, 3)
        ]
        assert defers == sorted(defers) and len(set(defers)) == 3, (
            f"retry delays {defers} do not back off; a persistent miss would hammer the queue"
        )

    def test_a_genuinely_absent_row_eventually_fails_loudly(self) -> None:
        """Retrying forever would hide a real deletion as an eternal 'pending'."""
        from arq.worker import Retry

        from aleph_workers.jobs.normalize import _missing_row

        exc = _missing_row({"job_try": 9}, "source version X not found")
        assert not isinstance(exc, Retry)
        assert isinstance(exc, RuntimeError)
        assert "9 attempts" in str(exc), (
            "the terminal error does not say how many attempts were made, so an "
            "operator cannot tell a lost race from a deleted row"
        )

    def test_missing_job_try_is_treated_as_the_first_attempt(self) -> None:
        """arq omits `job_try` in some paths; defaulting to 0 would skip retries."""
        from arq.worker import Retry

        from aleph_workers.jobs.normalize import _missing_row

        assert isinstance(_missing_row({}, "x"), Retry)
