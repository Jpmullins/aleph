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
"""

from __future__ import annotations

import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

#: The version this validator knows. A bundle declaring a different one is
#: reported rather than silently accepted — "it validated" would then mean
#: "it was checked against rules that may not be its own".
OKF_VERSION = "0.1"

RESERVED_STEMS = frozenset({"index", "log"})

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
            print(f"✓ {path}: {concepts} concept(s) conform to OKF v{OKF_VERSION}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
