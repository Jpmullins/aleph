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

3. **The sidecar round trip has to run over real claim text.** A claim is
   free text from a model and a source title is free text from a publisher;
   both reach `evidence.json` and the `## Evidence` section. `parse_evidence_json`
   reads the sidecar back and its bytes are re-rendered here, so a field the
   writer emits and the reader ignores shows up as a diff on the live corpus,
   not only over a fixture somebody chose.

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


def _sidecar_round_trip(label: str, files: dict[str, str]) -> list[str]:
    """Read `evidence.json` back and re-render it. Empty means no loss.

    The sidecar's whole selling point is that a dropped field shows up as a
    diff instead of as silence — and `parse_evidence_json` had no caller
    anywhere, so nothing ever read one back. Run here rather than only in a
    test because the strings that break a format are the ones a model wrote
    and a publisher titled, not the ones a fixture chose: this corpus holds
    claim text with brackets in it, quotes containing code fences, and titles
    nobody sanitised.
    """
    from aleph_wiki.export_evidence import (
        EVIDENCE_FILENAME,
        evidence_files,
        parse_evidence_json,
    )

    raw = files.get(EVIDENCE_FILENAME)
    if raw is None:
        return []
    try:
        header, pages = parse_evidence_json(raw)
    except ValueError as exc:
        return [f"{label}: {EVIDENCE_FILENAME} cannot be read back: {exc}"]
    # Re-rendered from the header the READER returned, not from the title this
    # process happens to be holding: that is what makes a header field the
    # writer stopped emitting visible.
    again = evidence_files(
        list(pages), project_title=header.project_title, dialect=header.dialect
    ).get(EVIDENCE_FILENAME)
    if again != raw:
        return [
            f"{label}: {EVIDENCE_FILENAME} does not survive a read and a re-render — "
            "a field is written and not read back"
        ]
    return []


def _markdown_round_trip(label: str, files: dict[str, str], title: str) -> list[str]:
    """Parse the pages back and re-render them. Empty means no loss.

    The other half of `_sidecar_round_trip`, and it was the missing half: on a
    LIVE corpus only the sidecar was read back, and `parse_vault` was exercised
    over fixtures alone. That is the wrong way round — the reason to run a round
    trip against real data is that the strings which break a format are the ones
    a model wrote and a publisher titled. This corpus holds page bodies with
    YAML-significant characters in the title, wikilinks inside code fences, and
    front-matter values a fixture would never think to choose.
    """
    from aleph_artifacts.exporters.vault import parse_vault, render_vault

    markdown = {name: body for name, body in files.items() if name.endswith(".md")}
    if not markdown:
        return []
    try:
        pages = parse_vault(markdown)
    except ValueError as exc:
        return [f"{label}: the exported markdown cannot be read back: {exc}"]
    again = render_vault(pages, dialect="okf", project_title=title).files
    if dict(again) != markdown:
        lost = sorted(set(markdown) ^ set(again)) or sorted(
            name for name, body in markdown.items() if again.get(name) != body
        )
        return [
            f"{label}: the markdown does not survive parse_vault + render_vault — "
            f"{len(lost)} file(s) differ, first: {lost[0] if lost else '?'}"
        ]
    return []


def _validate(label: str, files: dict[str, str], counts: object, title: str) -> list[str]:
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
    out += _sidecar_round_trip(label, files)
    out += _markdown_round_trip(label, files, title)
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
                          -- Must have something to EXPORT, not just something to
                          -- cite. This arm picked purely by anchored-citation
                          -- count, and a project whose pages are all stubs
                          -- exports zero concept documents — which `check-okf`
                          -- correctly refuses as an empty bundle. So the gate
                          -- flipped between PASS and FAIL on two identical runs
                          -- twenty minutes apart, with no change to the tree:
                          -- integration tests had created a new most-anchored
                          -- project in between. A gate whose subject is chosen
                          -- from live data has to constrain the choice to
                          -- subjects it can actually grade.
                          AND EXISTS (
                            SELECT 1 FROM wiki_pages w
                            WHERE w.project_id = p.id AND NOT w.is_stub
                          )
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
        problems += _validate(f"{label} ({title!r})", files, counts, title)
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
