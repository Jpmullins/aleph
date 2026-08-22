#!/usr/bin/env python3
"""Validate an exported vault against Open Knowledge Format v0.1.

Run over a directory or a .zip produced by
`POST /v1/projects/{id}/export/vault?dialect=okf`:

    uv run python scripts/check-okf.py path/to/vault.zip
    uv run python scripts/check-okf.py path/to/vault-dir

Exits 0 when the bundle conforms, 1 with a per-file report when it does not.

**Why a sweep and not a test.** The interoperability claim — "an Aleph project
opens as an Obsidian vault", and now "the okf dialect emits OKF v0.1" — was
asserted in three source comments for months with nothing anywhere writing a
vault at all. A claim about a *file format* is only worth what a validator says
about the actual bytes, so this reads the bundle rather than the code that
produced it. Point it at an export from the live corpus and it will tell you
what a third-party OKF reader would find.

**The rules, from the OpenWiki specification of OKF v0.1**
(https://docs.langchain.com/oss/openwiki/code-mode):

1. A *concept* is an ordinary markdown page with YAML front matter carrying a
   **non-empty `type`**. Other standard fields are optional.
2. `index.md` and `log.md` are **reserved scaffolding, not concepts**:
   `index.md` is a directory listing and `log.md` is update history. The root
   index declares `okf_version: "0.1"`.
3. Valid `timestamp` values and producer-defined extension fields are accepted
   and preserved.
4. **Standard markdown links** between concept documents express relationships.

Rules 1 and 2 are what the WS-H8 criterion names (a non-empty `type` on 100% of
files); 4 is why `[[wikilinks]]` are a failure here rather than a stylistic
preference — an OKF reader cannot follow one. A residual `[[` in an okf bundle
is reported even when it is really a Python list literal in a fenced block: an
OKF consumer cannot tell those apart either, so neither does this.

**One Aleph extension, clearly marked.** With `?evidence=true` (the default) the
bundle also carries `evidence.json`: the belief layer the pages were rendered
from — claims, and citations anchored to a verbatim quote at a character span
in a named source. OKF v0.1 says nothing about it, and the rules below whose
name starts with `evidence-` are therefore Aleph's, not the specification's.
They are here rather than in a second script because the criterion a person
runs is "check-okf over the export exits 0", and a sidecar no command validates
is a file format nobody is checking. They fire only when the sidecar is
present, so an ordinary OKF bundle is judged by OKF's rules alone.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

#: The version this validator knows. A bundle declaring a different one is
#: reported rather than silently accepted — "it validated" would then mean
#: "it was checked against rules that may not be its own".
OKF_VERSION = "0.1"

RESERVED_STEMS = frozenset({"index", "log"})

#: The Aleph evidence sidecar and the only version this validator knows. Kept
#: as literals rather than imported from `aleph_wiki.export_evidence`: this
#: script must run over a bundle somebody sent you, on a machine with no Aleph
#: checkout, which is the whole point of validating bytes instead of code.
EVIDENCE_FILENAME = "evidence.json"
EVIDENCE_VERSION = "1"

#: Same fence rule as `aleph_wiki.frontmatter`: only a block that opens the
#: file. A `---` further down is a horizontal rule, and treating it as a fence
#: would report a page's body as its metadata.
_FENCE = re.compile(r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

#: Obsidian-only. Rule 4 says relationships are standard markdown links.
_WIKILINK = re.compile(r"\[\[[^\]\[]+\]\]")

#: Intra-bundle links: `[text](./slug.md)`, optionally with an anchor. Absolute
#: paths and external URLs are somebody else's business.
_LOCAL_LINK = re.compile(r"\]\(\./(?P<stem>[A-Za-z0-9._-]+)\.md(?:#[^)\s]*)?\)")

#: Date-ish fields OKF says must carry valid timestamps when present.
_TIMESTAMP_FIELDS = ("timestamp", "created", "updated")


@dataclass(frozen=True, slots=True)
class Problem:
    file: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.file}: [{self.rule}] {self.detail}"


def _load(path: Path) -> dict[str, str]:
    """Read a bundle from a zip or a directory into name → text."""
    if path.is_dir():
        return {
            str(p.relative_to(path)): p.read_text(encoding="utf-8")
            for p in sorted(path.rglob("*"))
            if p.is_file()
        }
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return {
                name: zf.read(name).decode("utf-8")
                for name in sorted(zf.namelist())
                if not name.endswith("/")
            }
    msg = f"{path} is neither a directory nor a zip"
    raise SystemExit(msg)


def _frontmatter(text: str) -> dict[str, object] | None:
    match = _FENCE.match(text)
    if match is None:
        return None
    try:
        loaded = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _stem(name: str) -> str:
    return name.rsplit("/", 1)[-1][: -len(".md")]


def check_bundle(files: dict[str, str]) -> list[Problem]:
    """Every way this bundle fails OKF v0.1, not just the first."""
    problems: list[Problem] = []
    markdown = {name: text for name, text in files.items() if name.endswith(".md")}
    stems = {_stem(name) for name in markdown}

    # --- rule 2: the root index, and what it declares -----------------------
    index = files.get("index.md")
    if index is None:
        problems.append(Problem("index.md", "root-index", "the bundle has no root index.md"))
    else:
        fm = _frontmatter(index)
        declared = str((fm or {}).get("okf_version") or "").strip()
        if not declared:
            problems.append(
                Problem(
                    "index.md",
                    "okf-version",
                    'the root index does not declare okf_version: "0.1"',
                )
            )
        elif declared != OKF_VERSION:
            problems.append(
                Problem(
                    "index.md",
                    "okf-version",
                    f"declares OKF {declared!r}; this validator only knows {OKF_VERSION!r}",
                )
            )

    for name, text in sorted(markdown.items()):
        stem = _stem(name)

        # --- rule 4: no Obsidian-only syntax anywhere in the bundle ----------
        for hit in _WIKILINK.findall(text):
            problems.append(
                Problem(name, "wikilink", f"{hit} is an Obsidian wikilink, not a markdown link")
            )
        if "[[" in text and not _WIKILINK.search(text):
            problems.append(
                Problem(name, "wikilink", "contains `[[`, which an OKF reader cannot interpret")
            )

        # --- rule 4: a relationship must point at a document in the bundle ---
        for target in {m.group("stem") for m in _LOCAL_LINK.finditer(text)}:
            if target not in stems:
                problems.append(
                    Problem(name, "dangling-link", f"links to ./{target}.md, which is not here")
                )

        if stem in RESERVED_STEMS:
            # Reserved scaffolding, not a concept: no `type` is required, and
            # requiring one is how a validator ends up demanding that a
            # directory listing declare what kind of knowledge it holds.
            continue

        # --- rule 1: every concept carries front matter with a non-empty type
        fm = _frontmatter(text)
        if fm is None:
            problems.append(Problem(name, "front-matter", "no parseable YAML front matter"))
            continue
        if not str(fm.get("type") or "").strip():
            problems.append(Problem(name, "type", "front matter has no non-empty `type`"))
        if not str(fm.get("title") or "").strip():
            problems.append(Problem(name, "title", "front matter has no non-empty `title`"))

        # --- rule 3: timestamps, where present, must be valid ---------------
        for key in _TIMESTAMP_FIELDS:
            raw = fm.get(key)
            if raw in (None, ""):
                continue
            if isinstance(raw, (date, datetime)):
                continue
            try:
                datetime.fromisoformat(str(raw))
            except ValueError:
                problems.append(Problem(name, "timestamp", f"{key}={raw!r} is not a valid date"))

    problems.extend(_check_evidence(files, stems))
    return problems


def _check_evidence(files: dict[str, str], stems: set[str]) -> list[Problem]:
    """The Aleph evidence sidecar, if the bundle carries one.

    Not OKF v0.1 — see the module docstring. What is checked is the property
    the sidecar exists for: that the chain claim → citation → source → span is
    intact and points at documents that are actually in the bundle. A slug in
    `evidence.json` with no `slug.md` next to it is the same dangling reference
    the OKF link rule catches between pages, one level up, and it is the exact
    shape a stub filter that stopped matching would produce.
    """
    raw = files.get(EVIDENCE_FILENAME)
    if raw is None:
        return []
    name = EVIDENCE_FILENAME
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [Problem(name, "evidence-parse", f"is not valid JSON: {exc}")]
    if not isinstance(loaded, dict):
        return [Problem(name, "evidence-parse", "is not a JSON object")]
    document: dict[str, Any] = loaded

    problems: list[Problem] = []
    declared = str(document.get("aleph_evidence_version") or "").strip()
    if declared != EVIDENCE_VERSION:
        problems.append(
            Problem(
                name,
                "evidence-version",
                f"declares evidence format {declared!r}; this validator knows {EVIDENCE_VERSION!r}",
            )
        )
        # A version mismatch means the keys below may mean something else, so
        # reporting field-level problems against them would be guesswork.
        return problems

    pages = document.get("pages")
    if not isinstance(pages, list):
        return [*problems, Problem(name, "evidence-parse", "`pages` is not a list")]

    claims = citations = 0
    for entry in pages:
        if not isinstance(entry, dict):
            problems.append(Problem(name, "evidence-parse", "a page entry is not an object"))
            continue
        page: dict[str, Any] = entry
        slug = str(page.get("slug") or "").strip()
        if not slug:
            problems.append(Problem(name, "evidence-page", "a page entry carries no slug"))
        elif slug not in stems:
            problems.append(
                Problem(name, "evidence-page", f"names {slug}.md, which is not in the bundle")
            )
        for claim_raw in page.get("claims") or []:
            if not isinstance(claim_raw, dict):
                continue
            claim: dict[str, Any] = claim_raw
            claims += 1
            if not str(claim.get("text") or "").strip():
                problems.append(Problem(name, "evidence-claim", f"{slug}: a claim carries no text"))
            for citation_raw in claim.get("citations") or []:
                if not isinstance(citation_raw, dict):
                    continue
                citation: dict[str, Any] = citation_raw
                citations += 1
                start, end = citation.get("char_start"), citation.get("char_end")
                if isinstance(start, int) and isinstance(end, int) and end < start:
                    problems.append(
                        Problem(
                            name,
                            "evidence-span",
                            f"{slug}: a citation spans chars {start}-{end}, which is backwards",
                        )
                    )
                if (start is None) != (end is None):
                    problems.append(
                        Problem(
                            name,
                            "evidence-span",
                            f"{slug}: a citation carries half a character span",
                        )
                    )
    for key, counted in (("claim_count", claims), ("citation_count", citations)):
        stated = document.get(key)
        if isinstance(stated, int) and stated != counted:
            # A header count that disagrees with the body is how "the export
            # carries 8,056 claims" becomes a number nobody re-derived.
            problems.append(
                Problem(name, "evidence-count", f"{key} says {stated}; the file holds {counted}")
            )
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    failed = False
    for raw in argv:
        path = Path(raw)
        if not path.exists():
            print(f"check-okf: {path} does not exist", file=sys.stderr)
            return 2
        files = _load(path)
        problems = check_bundle(files)
        concepts = sum(1 for n in files if n.endswith(".md") and _stem(n) not in RESERVED_STEMS)
        if problems:
            failed = True
            print(f"✗ {path}: {len(problems)} OKF v0.1 problem(s) over {concepts} concept(s)")
            for problem in problems:
                print(f"    {problem}")
        else:
            # The evidence sidecar is named separately in the OK line, because
            # "conforms to OKF v0.1" must not be read as "and the evidence was
            # checked" when the bundle carries none — a green line that covers
            # a file that is not there is how a check stops meaning anything.
            carried = (
                " + a consistent evidence sidecar"
                if EVIDENCE_FILENAME in files
                else " (no evidence sidecar in this bundle)"
            )
            print(f"✓ {path}: {concepts} concept(s) conform to OKF v{OKF_VERSION}{carried}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
