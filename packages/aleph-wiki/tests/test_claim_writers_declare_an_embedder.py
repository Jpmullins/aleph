"""Every production claim write states, in source, whether it embeds.

`wiki_claims.embedding` is NULL on 26,296 live rows and the HNSW index over it
has never had anything to index. The diagnosis that produced this test was
that "seven `commit_revision` call sites pass no `embed=`". Reading them, five
pass a `claims` list that is empty by construction and two carry claims that
already exist, so the missing keyword is not what is keeping the column NULL —
the running image predates the wiring. But the shape of the defect is real and
it will come back: `embed` is an optional keyword with a `None` default, so a
new call site that DOES mint claims gets NULL vectors, passes review, passes
pyright, passes every test, and is discovered months later by a query.

So the rule is: at every production call, the embedder is either supplied or
visibly declined. `embed=None` is a passing answer — an author who wrote it
decided — and an omitted keyword is not.

**The exemption is provable, not asserted.** A call whose `claims=` argument is
the literal `[]` writes nothing (`write_claims` returns on an empty list), so
there is no vector to miss. That is read off the syntax tree, not from a list
of blessed files, which is the difference between an exemption and an
allowlist that rots.

This is a test rather than a `scripts/check-*.sh` because pytest already runs
everywhere the sweeps do and needs no wiring to be discovered — the failure
mode `scripts/check-sweeps-are-wired.sh` exists to catch.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

#: Repo root: .../packages/aleph-wiki/tests/<this file>
ROOT = Path(__file__).resolve().parents[3]

#: Functions that write a claim through the belief layer. `upsert_claim` is the
#: one that actually assigns `WikiClaim.embedding`; the other two are the doors
#: production reaches it through.
CLAIM_WRITERS = ("commit_revision", "write_claims", "upsert_claim")


def _production_files() -> list[Path]:
    """Tracked `.py` under packages/ and apps/, excluding tests.

    `git ls-files` rather than a glob, so a file sitting untracked in a working
    tree neither adds a spurious failure nor — more importantly — lets a real
    one hide until it is committed.
    """
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "packages/*.py", "apps/*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    paths = [ROOT / p for p in out.split("\0") if p]
    return [
        p
        for p in paths
        if "/tests/" not in str(p) and not p.name.startswith("test_") and "/alembic/" not in str(p)
    ]


def _writes_nothing(call: ast.Call) -> bool:
    """True when `claims=[]` is written literally at the call site.

    Only a literal. `claims=some_variable` may be empty today and is not empty
    by construction, and a check that accepted it would be reading the
    programmer's intention rather than the code.
    """
    for kw in call.keywords:
        if kw.arg == "claims":
            return isinstance(kw.value, ast.List) and not kw.value.elts
    return False


def _claim_write_calls() -> list[tuple[Path, ast.Call, str]]:
    found: list[tuple[Path, ast.Call, str]] = []
    for path in _production_files():
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not expected
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in CLAIM_WRITERS:
                found.append((path, node, str(name)))
    return found


def test_the_sweep_finds_the_call_sites_it_is_meant_to_guard() -> None:
    """A sweep that matches nothing passes forever.

    Pinned first, and with a floor rather than an exact count, because the
    whole class of defect this file is about is a green produced by not
    looking. If a refactor renames `commit_revision`, this fails instead of the
    rule silently ceasing to apply.
    """
    calls = _claim_write_calls()
    names = {name for _p, _c, name in calls}

    assert len(calls) >= 8, f"expected the wiki write path to be reachable; found {len(calls)}"
    assert names == set(CLAIM_WRITERS), (
        f"a claim writer has no production caller any more: missing {set(CLAIM_WRITERS) - names}"
    )


def test_every_claim_write_states_whether_it_embeds() -> None:
    offenders: list[str] = []
    for path, call, name in _claim_write_calls():
        if any(kw.arg == "embed" for kw in call.keywords):
            continue
        if _writes_nothing(call):
            continue
        offenders.append(f"{path.relative_to(ROOT)}:{call.lineno}  {name}(...)")

    assert not offenders, (
        "these claim writes neither pass `embed=` nor write a literal empty claim list, "
        "so any claim they mint lands with a NULL vector and is invisible to "
        "`search_claims`:\n  " + "\n  ".join(sorted(offenders)) + "\n\n"
        "Pass an embedder, or pass `embed=None` and say in a comment why this path "
        "cannot have one."
    )


def test_the_definitions_still_accept_the_keyword() -> None:
    """The call sites are only meaningful if the parameter exists.

    A rule enforced entirely on call syntax would keep passing if `embed` were
    renamed on the definitions and every caller updated to a keyword that no
    longer reaches `WikiClaim.embedding`.
    """
    for module, func in (
        ("packages/aleph-wiki/src/aleph_wiki/wiki_service.py", "commit_revision"),
        ("packages/aleph-wiki/src/aleph_wiki/wiki_service.py", "write_claims"),
        ("packages/aleph-wiki/src/aleph_wiki/belief_service.py", "upsert_claim"),
    ):
        tree = ast.parse((ROOT / module).read_text())
        definition = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == func
        )
        params = {a.arg for a in definition.args.kwonlyargs}
        assert "embed" in params, f"{module}::{func} no longer takes `embed`"
