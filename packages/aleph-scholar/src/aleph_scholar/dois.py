"""DOI extraction, normalization, and tri-state verification.

`verify_dois` batches through OpenAlex first (`filter=doi:a|b|c`, up to 50
per request) and corroborates OpenAlex misses with a per-DOI Crossref
lookup. Verdict discipline (spec WP-2 §1):

- `ok=True`  — at least one upstream resolves the DOI.
- `ok=False` — BOTH upstreams answered an authoritative 404.
- `ok=None`  — any network failure on the deciding upstream(s).
- `retracted=True` — OpenAlex `is_retracted` OR a Crossref retraction
  relation; `False` only when a resolving upstream verified not-retracted;
  `None` whenever `ok` is not `True`.
"""

from __future__ import annotations

import re

from aleph_scholar.crossref import CrossrefClient, CrossrefWork
from aleph_scholar.errors import ScholarUpstreamError
from aleph_scholar.openalex import OPENALEX_DOI_BATCH_SIZE, OpenAlexClient, OpenAlexWork
from aleph_scholar.types import DoiVerdict

# Registrant suffixes may legitimately contain balanced parentheses (e.g. the
# Lancet's `10.1016/s0140-6736(97)11096-0`), so `)` and `]` are captured and
# then trimmed only when unbalanced within the match.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+")
_TRAILING_PUNCT = ".,;:!?"
_DOI_URL_PREFIX_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)


def normalize_doi(raw: str) -> str:
    """Lowercase and strip doi.org / doi: prefixes."""
    doi = raw.strip()
    doi = _DOI_URL_PREFIX_RE.sub("", doi)
    doi = doi.removeprefix("doi:").removeprefix("DOI:")
    return doi.lower()


def _trim_match(candidate: str) -> str:
    """Trim trailing punctuation and unbalanced closing brackets.

    Bracket balances are maintained incrementally — recounting per stripped
    character is O(n²) on a pathological all-`)` tail (crafted page bodies
    reach this via the reviewer's DOI pass).
    """
    paren = candidate.count(")") - candidate.count("(")
    square = candidate.count("]") - candidate.count("[")
    while candidate:
        last = candidate[-1]
        if last in _TRAILING_PUNCT:
            candidate = candidate[:-1]
            continue
        if last == ")" and paren > 0:
            candidate = candidate[:-1]
            paren -= 1
            continue
        if last == "]" and square > 0:
            candidate = candidate[:-1]
            square -= 1
            continue
        break
    return candidate


def extract_dois(text: str) -> list[str]:
    """Extract normalized DOIs from free text, deduped preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _DOI_RE.finditer(text):
        doi = normalize_doi(_trim_match(match.group(0)))
        if doi and doi not in seen:
            seen.add(doi)
            out.append(doi)
    return out


def _verdict_from_openalex(doi: str, work: OpenAlexWork) -> DoiVerdict:
    return DoiVerdict(
        doi=doi,
        ok=True,
        retracted=work.is_retracted,
        title=work.ref.title or None,
        year=work.ref.year,
        openalex_id=work.ref.openalex_id,
        checked_via="openalex",
    )


def _verdict_from_crossref(doi: str, work: CrossrefWork, *, openalex_answered: bool) -> DoiVerdict:
    return DoiVerdict(
        doi=doi,
        ok=True,
        retracted=work.retracted,
        title=work.ref.title or None,
        year=work.ref.year,
        openalex_id=None,
        checked_via="crossref+openalex" if openalex_answered else "crossref",
    )


def _unverifiable(doi: str, *, checked_via: str) -> DoiVerdict:
    return DoiVerdict(
        doi=doi,
        ok=None,
        retracted=None,
        title=None,
        year=None,
        openalex_id=None,
        checked_via=checked_via,
    )


def _missing(doi: str) -> DoiVerdict:
    return DoiVerdict(
        doi=doi,
        ok=False,
        retracted=None,
        title=None,
        year=None,
        openalex_id=None,
        checked_via="crossref+openalex",
    )


async def verify_dois(
    dois: list[str], *, openalex: OpenAlexClient, crossref: CrossrefClient
) -> list[DoiVerdict]:
    """Verify DOIs with batched OpenAlex resolution + Crossref corroboration."""
    normalized = [normalize_doi(d) for d in dois]
    unique: list[str] = []
    seen: set[str] = set()
    for doi in normalized:
        if doi and doi not in seen:
            seen.add(doi)
            unique.append(doi)

    found: dict[str, OpenAlexWork] = {}
    openalex_answered = True
    try:
        for start in range(0, len(unique), OPENALEX_DOI_BATCH_SIZE):
            batch = unique[start : start + OPENALEX_DOI_BATCH_SIZE]
            found.update(await openalex.works_by_dois(batch))
    except ScholarUpstreamError:
        openalex_answered = False

    verdicts: dict[str, DoiVerdict] = {}
    for doi in unique:
        work = found.get(doi)
        if work is not None:
            verdicts[doi] = _verdict_from_openalex(doi, work)
            continue
        # OpenAlex miss (or OpenAlex down) — corroborate via Crossref.
        try:
            cr_work = await crossref.get_work(doi)
        except ScholarUpstreamError:
            verdicts[doi] = _unverifiable(
                doi, checked_via="crossref+openalex" if openalex_answered else "crossref"
            )
            continue
        if cr_work is not None:
            verdicts[doi] = _verdict_from_crossref(
                doi, cr_work, openalex_answered=openalex_answered
            )
        elif openalex_answered:
            verdicts[doi] = _missing(doi)  # authoritative 404 on BOTH upstreams
        else:
            # Crossref says missing but OpenAlex never answered — not authoritative.
            verdicts[doi] = _unverifiable(doi, checked_via="crossref")

    return [verdicts[doi] for doi in normalized if doi]
