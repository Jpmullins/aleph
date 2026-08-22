"""Export a real vault with its evidence chain, and validate the bytes.

`scripts/check-okf.py` reads a bundle and says whether a third-party OKF reader
would accept it. It had no consumer: not `ci.yml`, not `acceptance.sh`, not
`self_check.sh` — the only mention anywhere was a line of prose in the plan.
`check-sweeps-are-wired.sh` globbed `scripts/check-*.sh`, so the repo's own
guard against unwired sweeps could not see a `.py` one.

This is the consumer, and it is deliberately not a unit test. Two things can
only be measured against a live corpus:

1. **The evidence chain has to be populated.** The one bundle the first pass
   cited as green carried 542 citations and **zero anchored** — every citation
   rendered `*unverified*`, with no quote, no span and no chunk, so the
   `evidence-span` and `evidence-count` rules were structurally no-ops on it.
   The two projects where the chain IS populated were never validated. So this
   picks the project with the MOST anchored citations and refuses to report a
   pass if that number is zero.

2. **The marker shape is a property of the writer, not of a fixture.**
   Production stores `[c4]`; every fixture wrote `c4` straight into the row,
   bypassing the writer. The exporter wrapped the stored value in brackets
   again, so a real export emitted `**[[c4]]**` — an Obsidian wikilink to a
   page that does not exist, once per citation, 542 of them in one project.
   163 tests passed over a format corrupt in 100% of real exports.

It goes through the library rather than the HTTP route on purpose: the API runs
from a baked image, so a route-based probe measures whatever was current when
that image was built.

Exit 0 pass · 1 fail · 2 could not run (no database, or no corpus to export).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

#: How many anchored citations a bundle must carry before its evidence rules
#: mean anything. One is enough to make `evidence-span` non-vacuous; the point
#: is that zero is not.
MIN_ANCHORED = 1


async def _bundle(session: object, project_id: object, title: str) -> tuple[dict[str, str], object]:
    """Render one project the way the export route does."""
    from aleph_artifacts.exporters.vault import render_vault
    from aleph_wiki.export_evidence import count_evidence, evidence_files
    from aleph_wiki.export_service import load_page_evidence

    evidence_by_page = await load_page_evidence(session, project_id)  # pyright: ignore[reportArgumentType]
    pages = await _vault_pages(session, project_id, evidence_by_page)
    export = render_vault(pages, dialect="okf", project_title=title)  # pyright: ignore[reportArgumentType]
    page_evidence = sorted(evidence_by_page.values(), key=lambda p: (p.slug, p.title))
    counts = count_evidence(page_evidence)
    extra = evidence_files(page_evidence, project_title=title, dialect=export.dialect)
    return {**export.files, **extra}, counts


def _validate(label: str, files: dict[str, str], counts: object) -> list[str]:
    """Every problem with this bundle, as lines. Empty means it conforms."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_okf", pathlib.Path(__file__).resolve().parents[1] / "check-okf.py"
    )
    assert spec and spec.loader
    check_okf = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `check-okf.py` uses `from __future__ import
    # annotations`, so `@dataclass` resolves its field types by looking the
    # module up in `sys.modules`, and a module that is not there yet resolves
    # to None.
    sys.modules[spec.name] = check_okf
    spec.loader.exec_module(check_okf)

    out: list[str] = []
    # The invented-wikilink regression, measured on real bytes rather than on a
    # fixture: nothing in the okf dialect may contain `[[`.
    invented = sorted(name for name, body in files.items() if "[[" in body)
    if invented:
        out.append(
            f"{label}: {len(invented)} file(s) carry Obsidian wikilink syntax in "
            f"the okf dialect: {', '.join(invented[:5])}"
        )
    concepts = sum(
        1 for n in files if n.endswith(".md") and check_okf._stem(n) not in check_okf.RESERVED_STEMS
    )
    out += [f"{label}: {problem}" for problem in check_okf.check_bundle(files)]
    if not concepts:
        out.append(f"{label}: the export produced no concept documents")
    return out


async def _run() -> int:
    url = os.environ.get("DATABASE_URL") or os.environ.get("ALEPH_DATABASE_URL")
    if not url:
        print("okf-export: no DATABASE_URL — cannot export a real vault")
        return 2

    engine = create_async_engine(url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            # TWO bundles, because one cannot answer both questions.
            #
            # The project with the most PAGES proves the format holds at the
            # size a real wiki reaches. The project with the most ANCHORED
            # citations proves the evidence rules are not vacuous — the bundle
            # the first pass cited as green carried 542 citations and zero
            # anchored, so `evidence-span` and `evidence-count` had nothing to
            # check. Validating only the big one repeats that; validating only
            # the anchored one currently means validating a single page.
            #
            # Soft-deleted projects are excluded. `deleteProject` in the browser
            # suite sets `status = 'deleted'` and leaves the rows, so 76 dead
            # e2e projects held every anchored citation in the database.
            biggest = (
                await session.execute(
                    text("""
                        SELECT p.id, p.title, COUNT(*) AS pages
                        FROM projects p
                        JOIN wiki_pages w ON w.project_id = p.id
                        WHERE p.status <> 'deleted' AND NOT w.is_stub
                        GROUP BY p.id, p.title
                        ORDER BY pages DESC
                        LIMIT 1
                    """)
                )
            ).first()
            anchored_best = (
                await session.execute(
                    text("""
                        SELECT p.id, p.title,
                               COUNT(*) FILTER (
                                 WHERE c.quote IS NOT NULL AND c.char_start IS NOT NULL
                               ) AS anchored
                        FROM projects p
                        JOIN citations c ON c.project_id = p.id
                        WHERE p.status <> 'deleted'
                        GROUP BY p.id, p.title
                        ORDER BY anchored DESC
                        LIMIT 1
                    """)
                )
            ).first()

            if biggest is None:
                print("okf-export: no project has any non-stub pages — nothing to export")
                return 2

            targets = [("largest", biggest[0], biggest[1])]
            if anchored_best is not None and anchored_best[0] != biggest[0]:
                targets.append(("most-anchored", anchored_best[0], anchored_best[1]))

            rendered = [
                (label, title, *await _bundle(session, pid, title)) for label, pid, title in targets
            ]
    finally:
        await engine.dispose()

    problems: list[str] = []
    summary: list[str] = []
    total_anchored = 0
    for label, title, files, counts in rendered:
        problems += _validate(f"{label} ({title!r})", files, counts)
        pages = sum(1 for n in files if n.endswith(".md"))
        total_anchored += counts.anchored_citations  # pyright: ignore[reportAttributeAccessIssue]
        summary.append(
            f"{label} {title!r}: {pages} file(s), "
            f"{counts.anchored_citations}/{counts.citations} anchored"  # pyright: ignore[reportAttributeAccessIssue]
        )

    if problems:
        print(f"✗ okf-export: {len(problems)} problem(s)")
        for problem in problems[:12]:
            print(f"    {problem}")
        return 1

    if total_anchored < MIN_ANCHORED:
        print(
            "okf-export: every bundle validates, but NO citation anywhere is "
            "anchored, so the evidence rules were vacuous. "
            f"({'; '.join(summary)}) Run BeliefService.rebuild — decisions.md D9."
        )
        return 2

    print(f"✓ okf-export: OKF v0.1 conformant — {'; '.join(summary)}")
    return 0


async def _vault_pages(session: object, project_id: object, evidence_by_page: object) -> object:
    """The route's own page assembly, imported rather than reimplemented."""
    from aleph_api.routes.wiki import _vault_pages as build

    return await build(session, project_id, evidence_by_page)  # pyright: ignore[reportArgumentType]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory():
        raise SystemExit(asyncio.run(_run()))
