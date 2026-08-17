"""Literature-review helpers, bound to aleph-scholar.

The instructions in SKILL.md are ported verbatim from claude-science
(Apache-2.0) — see NOTICE. The helper implementations are NOT ported: Aleph
already has `aleph-scholar`, which does the same work with a polite-pool
throttle, a retry transport, tri-state DOI verdicts and a monthly Consensus
cap. Duplicating that here would give the skill a second, worse implementation
of the thing the package exists for.

So these are thin adapters. Each takes the project's `ScholarService` as its
first argument; the harness binds it.

Module top level is definition-only, so `aleph_kernel.ast_gate` admits this
file — the same discipline the upstream skill follows, and the upstream file
passes our gate unmodified.
"""

from __future__ import annotations

import re

# The trailing class excludes characters that commonly abut a DOI in prose
# (em dash, en dash, brackets) but are not part of it. Written as escapes:
# a dash class made of literal dashes is unreadable and easy to corrupt.
DOI_PATTERN = "10\\.\\d{4,9}/[^\\s\"'`\\]\\}\u2014\u2013&|]+"

_DOI_RE = re.compile(DOI_PATTERN)


def extract_dois(text: str) -> list[str]:
    """Every DOI appearing in free text, in order, deduplicated.

    Local and synchronous: finding a DOI is a regex, and making it a network
    call would mean a draft cannot be checked offline.
    """
    seen: list[str] = []
    for match in _DOI_RE.findall(text or ""):
        normalized = match.rstrip(".,;:)]}").lower()
        if normalized not in seen:
            seen.append(normalized)
    return seen


async def verify_dois(scholar, dois: list[str]) -> list:
    """Tri-state verification for each DOI.

    `ok=True` resolves, `ok=False` is an authoritative 404 on BOTH registries,
    `ok=None` could not be checked. The three-way answer is the point: treating
    "could not check" as "fabricated" would flag real papers whenever the
    network hiccups, and treating it as "fine" would let fabrications through.
    """
    return await scholar.verify_dois(dois)


async def crossref_lookup(scholar, query: str, rows: int = 10) -> list:
    """Find a DOI from a free-text citation.

    Use when you have author, year and title but not the identifier. This is
    the alternative to pattern-completing a DOI, which produces something that
    looks right and resolves to nothing.
    """
    return await scholar.crossref_lookup(query, rows=rows)


async def search_openalex(scholar, query: str, per_page: int = 10) -> list:
    """Relevance search over OpenAlex works."""
    return await scholar.search_openalex(query, per_page=per_page)


async def expand_citations(scholar, ref: str, direction: str = "both", limit: int = 25):
    """Walk one step of the citation graph, ranked by influence.

    Backward gives the work a paper is built on; forward gives what took it up.
    Both are ranked by citation count, so `limit` keeps the papers a field is
    built on rather than an arbitrary slice of the bibliography.
    """
    return await scholar.expand_citations(ref, direction=direction, limit=limit)


def style_pass(markdown: str) -> str:
    """Deterministic citation renumbering and reference rebuild. No model call.

    A lint, not a gate: run it once on the finished draft and fix what it
    changes. Do not loop until it stops changing things.
    """
    from aleph_scholar.style import style_pass as _style_pass

    return _style_pass(markdown)
