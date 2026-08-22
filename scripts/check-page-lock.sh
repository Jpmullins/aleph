#!/usr/bin/env bash
#
# A wiki page that is read without a row lock is a lost commit waiting to
# happen.
#
# `WikiService.commit_revision` picks a version number by asking "what is the
# highest so far?" and adding one, against UNIQUE(page_id, revision_no). That
# arithmetic is only safe while the page row is locked FOR UPDATE. It was not:
# the by-title branch of `_lock_or_create_page` did a plain SELECT and returned
# the row unlocked, so two concurrent commits both read max=N, both inserted
# N+1, and the loser's work was discarded as an unhandled 500. The same plain
# SELECT lost the create race too — two commits minting the same new title both
# saw "does not exist" and collided on uq_wiki_pages_project_slug.
#
# This is on the agent path, not a corner: four call sites pass a literal
# `page_id=None`, and the wiki ingest workflow passes a `UUID | None` that is
# None for any page it is minting, so concurrent ingest of two sources that name
# the same topic races every time.
#
# What this sweep asserts about `_lock_or_create_page`:
#
#   1. every `select(WikiPage)` in it is chained to `.with_for_update()`;
#   2. that lock is EXCLUSIVE — `with_for_update(read=True)` is FOR SHARE, and
#      shared locks do not serialise `max + 1`, so two holders both read the
#      same maximum. An adversarial review defeated the first version of this
#      sweep with exactly that edit: it matched the method name and never
#      looked at the mode, then printed "all FOR UPDATE", which was false;
#   3. no OTHER way of reading a page row appears in the function.
#      `session.get(WikiPage, page_id)` is an unlocked read that contains no
#      `select(` for rule 1 to find. The same review used it to slip an
#      unlocked read past the whole gate — the concurrency tests missed it too,
#      because none of them exercises the by-id branch;
#   4. the create is an `ON CONFLICT ... DO NOTHING` upsert, not a
#      SELECT-then-INSERT, so create-or-lock is one atomic step.
#
# Static: no database, no gateway, no running service. It cannot tell you the
# lock works — `tests/integration/test_commit_revision_concurrency.py` does that
# with eight real sessions — but it does catch the edit that quietly removes it,
# which is the shape this defect had for the life of the function.
#
# Fails on: an unlocked WikiPage read in the create-or-lock path, or the upsert
# reverted to a plain SELECT. Both mutations were run; both exit 1.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import ast
import pathlib
import sys

TARGET = pathlib.Path("packages/aleph-wiki/src/aleph_wiki/wiki_service.py")
FUNC = "_lock_or_create_page"

if not TARGET.exists():
    print(
        f"✗ {TARGET} does not exist — this sweep is not looking where the code is",
        file=sys.stderr,
    )
    raise SystemExit(1)

tree = ast.parse(TARGET.read_text(), filename=str(TARGET))

func: ast.AsyncFunctionDef | ast.FunctionDef | None = None
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == FUNC:
        func = node
        break

if func is None:
    # Renaming or inlining the function is not a way to pass this sweep. If the
    # create-or-lock path moves, this sweep moves with it — deliberately, in the
    # same change, so somebody has to look at the locking again.
    print(
        f"✗ {TARGET}: no `{FUNC}` — the create-or-lock path moved and this sweep "
        "was not updated",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _is_wikipage_select(node: ast.AST) -> bool:
    """True for `select(WikiPage)` / `select(WikiPage.x, ...)`."""
    if not isinstance(node, ast.Call):
        return False
    if not (isinstance(node.func, ast.Name) and node.func.id == "select"):
        return False
    for arg in node.args:
        root = arg
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id == "WikiPage":
            return True
    return False


def _receiver_calls(node: ast.Call) -> list[ast.AST]:
    """Walk a fluent chain `select(...).where(...).with_for_update()` back to its root."""
    chain: list[ast.AST] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        chain.append(cur)
        cur = cur.func.value
    chain.append(cur)
    return chain


selects = [n for n in ast.walk(func) if _is_wikipage_select(n)]
locked: set[int] = set()
shared: list[int] = []
upserts = 0
problems: list[str] = []

for node in ast.walk(func):
    if not isinstance(node, ast.Call):
        continue
    if isinstance(node.func, ast.Attribute) and node.func.attr == "with_for_update":
        # FOR SHARE is not a lock for this purpose. Two holders of a shared
        # lock both read the same `max(revision_no)` and both write max+1.
        exclusive = True
        for kw in node.keywords:
            if kw.arg == "read":
                exclusive = not (isinstance(kw.value, ast.Constant) and kw.value.value is True)
        for link in _receiver_calls(node):
            if _is_wikipage_select(link):
                if exclusive:
                    locked.add(id(link))
                else:
                    shared.append(node.lineno)
    if isinstance(node.func, ast.Attribute) and node.func.attr.startswith("on_conflict_do_"):
        upserts += 1

    # Rule 3: any other way of getting a page row.
    #
    # `session.get(WikiPage, ...)` issues an unlocked SELECT and contains no
    # `select(` for rule 1 to find. `scalar()`/`scalars()` over a bare
    # `WikiPage` query is the same shape. Named individually rather than
    # blanket-banning attribute calls, so the message says what to do instead.
    if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "get_one"}:
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id == "WikiPage":
                problems.append(
                    f"{TARGET}:{node.lineno} `session.{node.func.attr}(WikiPage, ...)` is an "
                    "UNLOCKED read. It bypasses this sweep's select() rule entirely and "
                    "reintroduces the exact race: use "
                    "`select(WikiPage).where(...).with_for_update()`."
                )

for lineno in shared:
    problems.append(
        f"{TARGET}:{lineno} `with_for_update(read=True)` is FOR SHARE, not FOR UPDATE. "
        "Shared locks do not serialise `revision_no = max + 1`: two holders both "
        "read the same maximum and the loser gets an IntegrityError on "
        "uq_wiki_rev_page_no."
    )

if not selects:
    problems.append(
        f"`{FUNC}` contains no `select(WikiPage)` at all — either the sweep is "
        "parsing the wrong thing, or the lock it exists to protect is gone"
    )

for node in selects:
    if id(node) not in locked:
        problems.append(
            f"{TARGET}:{node.lineno} `select(WikiPage)` with no `.with_for_update()`. "
            "This hands back an unlocked row, and `revision_no = max + 1` downstream "
            "then races: two concurrent commits both read the same max and the loser "
            "gets an IntegrityError on uq_wiki_rev_page_no."
        )

if upserts == 0:
    problems.append(
        f"{TARGET}:{func.lineno} `{FUNC}` has no `ON CONFLICT DO NOTHING` upsert. "
        "Create-or-fetch as SELECT-then-INSERT is two steps, and two concurrent "
        "commits minting the same new title both see 'does not exist' and collide "
        "on uq_wiki_pages_project_slug."
    )

if problems:
    print("✗ wiki page lock:", file=sys.stderr)
    for p in problems:
        print(f"   {p}", file=sys.stderr)
    print(
        "   See tests/integration/test_commit_revision_concurrency.py for what "
        "goes wrong with eight real sessions.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"✓ wiki page lock: {len(selects)} WikiPage read(s) in {FUNC}, all FOR UPDATE "
    f"(exclusive), no unlocked reads, create is an atomic upsert"
)
PY
