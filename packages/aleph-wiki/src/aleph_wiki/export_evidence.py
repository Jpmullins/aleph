"""The evidence chain, written into a vault export.

`aleph_artifacts.exporters.vault` exports the *prose*: one markdown file per
page, frontmatter, links. That is the wiki as a reader sees it, and on its own
it is the wrong half of the product. Aleph's thesis is that a page is
**rendered from** a belief layer — durable claims, each anchored to a verbatim
quote at an exact character span in a named source — so an export that carries
only the rendered prose exports the conclusions and drops the reason to believe
them. A vault like that cannot be checked, re-derived, or re-imported as
knowledge; it can only be read.

This module is the other half, and it is pure: dataclasses in, strings out, no
ORM and no session. `export_service.load_page_evidence` does the query.

**Two carriers, because there are two readers.**

* An `## Evidence` section appended to each page's markdown. A person (or an
  agent reading the vault as durable context) sees the claims and the quotes
  next to the prose they justify. It is standard markdown, so it survives OKF
  v0.1 (`scripts/check-okf.py`) and opens in Obsidian.
* `evidence.json`, one file per bundle. The section is a rendering and loses
  precision — a UUID and a `[start, end)` span are not prose. The sidecar is
  the lossless form, so a consumer can rebuild the chain
  claim → citation → source → chunk → character span without parsing markdown.

`parse_evidence_json` reads the sidecar back, and
`render_evidence_json(parse_evidence_json(x)) == x` is asserted in the tests.
That is the same round-trip discipline the vault format uses, for the same
reason: a dropped field shows up as a diff instead of as silence.

**Nothing here carries a timestamp.** An export stamped with "generated at" is
never byte-identical to the next one, which destroys the round-trip check that
is the only evidence the format loses nothing. When the export happened is a
ledger question, and the route writes a ledger row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

__all__ = [
    "EVIDENCE_FILENAME",
    "EVIDENCE_FORMAT_VERSION",
    "ClaimEvidence",
    "EvidenceCitation",
    "EvidenceCounts",
    "PageEvidence",
    "attach_evidence",
    "count_evidence",
    "evidence_files",
    "parse_evidence_json",
    "render_evidence_json",
    "render_evidence_section",
    "strip_evidence_section",
]

#: The sidecar. Not a `.md` file on purpose: OKF v0.1 says every markdown
#: document that is not `index.md` or `log.md` is a *concept*, so shipping the
#: evidence as markdown would add a concept to the vault that is not a page and
#: would inflate every count taken over the bundle.
EVIDENCE_FILENAME = "evidence.json"

#: Bumped when the sidecar's shape changes incompatibly. A consumer that reads
#: a version it does not know should say so rather than guess — which is why
#: this is a declared field and not something inferred from the keys present.
EVIDENCE_FORMAT_VERSION = "1"

#: Delimits the generated block inside a page body. `attach_evidence` removes
#: an existing block before writing a new one, so exporting a body that already
#: carries a section is idempotent instead of appending a second copy. That is
#: not hypothetical: the iterate step of WS-H8 is a worker that keeps a vault
#: continuously in sync, and re-export over a previous export is its steady
#: state.
MARKER_OPEN = "<!-- aleph:evidence -->"
MARKER_CLOSE = "<!-- /aleph:evidence -->"

_BLOCK = re.compile(
    re.escape(MARKER_OPEN) + r".*?" + re.escape(MARKER_CLOSE) + r"[ \t]*\r?\n?",
    re.DOTALL,
)

#: A line that opens or closes a fenced code block, by the same rule
#: `aleph_artifacts.exporters.vault._FENCE_LINE` uses. Quotes are rendered
#: inside a fence (see `_quote_block`) and a quote containing one of these
#: would close that fence early — see the comment there, this is not cosmetic.
_FENCE_LINE = re.compile(r"^[ \t]*(?:```|~~~)")

#: Longest run of backticks in a string, so the fence can be made longer than
#: anything inside it. CommonMark closes a fence only on a run at least as long
#: as the opener.
_BACKTICKS = re.compile(r"`+")


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    """One anchored piece of evidence, flattened away from the ORM.

    `source_short_id` is the human-facing name of the source (`S0002`) and
    `source_id` is the UUID. Both are carried: the short id is what a person
    reading the vault can look up, and the UUID is what a re-import needs. A
    format that carries only one of them either reads badly or imports badly.
    """

    #: The citation's label WITHOUT brackets — `c4`, not `[c4]`.
    #:
    #: Production stores it bracketed: `claim_extraction.py` writes
    #: `citation_marker=f"[c{number}]"`, `belief_service` defaults to `"[c1]"`,
    #: and 8,017 of 8,081 markers in the live database start with `[`.
    #: `_citation_line` wrapped it in brackets again, so a real export rendered
    #: `**[[c4]]**` — an Obsidian wikilink to a page that does not exist, one
    #: per citation, 542 of them in one project. In the OKF dialect the link
    #: resolver could not find slug `c4` and stripped the brackets instead, so
    #: the token joining a sentence to its citation was silently rewritten.
    #:
    #: Every fixture wrote the unbracketed shape directly into the row,
    #: bypassing the production writer, which is why 163 tests passed over a
    #: format that was corrupt in 100% of real exports. `normalize_marker` is
    #: applied at the one boundary where the ORM value enters, so there is a
    #: single shape downstream rather than a `.strip("[]")` at each reader —
    #: `aleph_reviewer.mechanical.workflow` already carries one of those.
    marker: str
    stance: str
    weight: float
    verbatim: bool
    source_id: str | None = None
    source_short_id: str | None = None
    source_title: str | None = None
    source_url: str | None = None
    chunk_id: str | None = None
    quote: str | None = None
    char_start: int | None = None
    char_end: int | None = None

    @property
    def anchored(self) -> bool:
        """The whole chain is present: a source, a quote, and where it sits.

        This is the property that decides whether the export carries knowledge
        or a bibliography. A citation with a source and no quote says "this
        page cites that paper"; an anchored one says "this sentence, at these
        offsets, is why".
        """
        return bool(
            self.source_id
            and self.quote
            and self.char_start is not None
            # `is not None`, not truthiness: an offset of 0 is the first
            # character of a document, and `if self.char_end` reports the
            # best-anchored citation in the corpus as unanchored.
            and self.char_end is not None
        )


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    """One durable claim and the citations that anchor it."""

    claim_id: str
    text: str
    confidence: str
    evidence_tier: str
    origin: str
    status: str
    section_anchor: str | None = None
    citations: tuple[EvidenceCitation, ...] = ()


@dataclass(frozen=True, slots=True)
class PageEvidence:
    """Every live claim on one page."""

    slug: str
    title: str
    claims: tuple[ClaimEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceCounts:
    """What a caller is told about an export before downloading it.

    `anchored_citations` is reported separately from `citations` because the
    two answer different questions and the difference is the whole point of the
    workstream: 8,079 citations exist on the live corpus and 4,082 of them carry
    a quote and a span. A single "citations" number would report that corpus as
    fully evidenced.
    """

    claims: int = 0
    citations: int = 0
    anchored_citations: int = 0
    pages_with_claims: int = 0


def count_evidence(pages: Iterable[PageEvidence]) -> EvidenceCounts:
    claims = citations = anchored = with_claims = 0
    for page in pages:
        if page.claims:
            with_claims += 1
        for claim in page.claims:
            claims += 1
            for citation in claim.citations:
                citations += 1
                if citation.anchored:
                    anchored += 1
    return EvidenceCounts(
        claims=claims,
        citations=citations,
        anchored_citations=anchored,
        pages_with_claims=with_claims,
    )


# --- the markdown section ---------------------------------------------------


def _not_inlined(reason: str) -> str:
    """The line that stands in for a quote the section cannot safely inline.

    It names the reason and where the quote actually is. A bare "omitted" would
    read as a bug in the export, and a silent omission would read as a claim
    with no quote — which is a different and worse statement, because that is
    what an unverified citation looks like.
    """
    return (
        f"  *(quote {reason} and is carried verbatim in "
        f"`{EVIDENCE_FILENAME}` rather than inlined here)*"
    )


def _quote_block(quote: str) -> list[str]:
    """A verbatim quote, rendered so nothing between here and a reader edits it.

    Fenced, and the fence is one backtick longer than the longest run inside
    the quote, because a quote about markdown contains backticks.

    The fence is what makes "verbatim" true of the *rendering*, not only of the
    stored bytes. A source sentence is prose somebody else wrote and it is full
    of characters markdown treats as instructions: a line starting with `#`
    becomes a heading, `- ` becomes a list, `*word*` becomes emphasis with the
    asterisks deleted. Pasted unfenced under a claim that says "verbatim", that
    is a quote the reader cannot trust and, in the emphasis case, one whose
    characters are genuinely gone. Inside a fence every byte is shown.

    Two quotes are NOT inlined:

    * one whose own line opens a fence. It would close this block early and
      desync `render_vault`'s fence tracker for the *rest of the page*, which
      silently switches off wikilink rewriting from there down — a corruption
      of the export well outside this section.
    * one containing `[[`. The exporter would leave it alone inside the fence,
      which is right, and `scripts/check-okf.py` would then report the okf
      bundle, deliberately: "an OKF consumer cannot tell a wikilink from a
      Python list literal in a fenced block either, so neither does this".
      Escaping it would satisfy the validator by editing somebody's words,
      which is the trade this module exists to refuse.

    Either way `evidence.json` still carries the quote byte-exact, which is
    what the sidecar is for. Neither rule is dialect-specific on purpose: the
    vault exporter's contract is that the two dialects differ in link *syntax*
    and nothing else, so a quote inlined in one bundle and omitted from the
    other would break an invariant to save a rare line.
    """
    if any(_FENCE_LINE.match(line) for line in quote.splitlines()):
        return [_not_inlined("contains a code fence")]
    if "[[" in quote:
        return [_not_inlined("contains a wikilink-shaped bracket pair")]
    longest = max((len(m.group(0)) for m in _BACKTICKS.finditer(quote)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", *quote.splitlines(), fence]


def _source_label(citation: EvidenceCitation) -> str:
    """`S0002` *Attention Is All You Need* — whichever of those exist.

    A citation whose source row is gone still renders, naming the UUID. It is
    evidence pointing at something that was deleted, and saying so is more
    useful than dropping the line.
    """
    parts: list[str] = []
    if citation.source_short_id:
        parts.append(f"`{citation.source_short_id}`")
    if citation.source_title:
        parts.append(citation.source_title.strip())
    if not parts:
        parts.append(f"source `{citation.source_id}`" if citation.source_id else "source unknown")
    return " ".join(parts)


def normalize_marker(raw: str | None) -> str:
    """`[c4]` → `c4`. The one place the stored shape is unwrapped.

    Idempotent, so a marker already bare survives, and a marker that is neither
    shape is returned as-is rather than mangled — a citation labelled `4` or
    `fig-2` is not this function's business.
    """
    text = (raw or "").strip()
    while text.startswith("[") and text.endswith("]") and len(text) > 2:
        text = text[1:-1].strip()
    return text


def _citation_line(citation: EvidenceCitation) -> str:
    bits = [f"**[{citation.marker}]**", citation.stance, _source_label(citation)]
    if citation.char_start is not None and citation.char_end is not None:
        bits.append(f"chars {citation.char_start}-{citation.char_end}")
    if citation.chunk_id:
        bits.append(f"chunk `{citation.chunk_id}`")
    if not citation.verbatim:
        # A citation nobody located in its source. Said out loud, because the
        # rest of this section reads as though every quote was checked.
        bits.append("*unverified*")
    if citation.source_url:
        # Angle brackets, not a markdown link: `[text](url)` next to a bundle
        # full of `[text](./slug.md)` links invites a reader — and the OKF
        # dangling-link rule — to treat it as an intra-vault reference.
        bits.append(f"<{citation.source_url}>")
    return " · ".join(bits)


def render_evidence_section(page: PageEvidence) -> str:
    """The `## Evidence` block for one page, delimited by the markers.

    Returns `""` for a page with no claims: a heading over nothing reads as
    "checked, found nothing", and what it actually means is that the belief
    layer has never run over this page. The dry-run report carries the counts,
    which is where "measured and zero" belongs.
    """
    if not page.claims:
        return ""
    counts = count_evidence([page])
    lines = [
        MARKER_OPEN,
        "",
        "## Evidence",
        "",
        (
            f"Rendered from Aleph's claim spine: {counts.claims} "
            f"claim{'' if counts.claims == 1 else 's'}, {counts.citations} "
            f"citation{'' if counts.citations == 1 else 's'}, "
            f"{counts.anchored_citations} anchored to a verbatim quote at a "
            "character span in the cited source. Generated — edit the claims, "
            "not this section."
        ),
        "",
    ]
    for claim in page.claims:
        meta = [
            f"`confidence: {claim.confidence}`",
            f"`evidence: {claim.evidence_tier}`",
            f"`origin: {claim.origin}`",
        ]
        if claim.status != "active":
            # A retracted claim stays in the export — "we believed this and
            # withdrew it" is knowledge — but it must never read as support.
            meta.append(f"`status: {claim.status}`")
        if claim.section_anchor:
            meta.append(f"`section: {claim.section_anchor}`")
        meta.append(f"`claim: {claim.claim_id}`")
        lines += [f"**Claim.** {claim.text.strip()}", "", " · ".join(meta), ""]
        for citation in claim.citations:
            lines += [_citation_line(citation), ""]
            if citation.quote:
                lines += [*_quote_block(citation.quote), ""]
        if not claim.citations:
            lines += ["*No citation anchors this claim.*", ""]
    lines.append(MARKER_CLOSE)
    return "\n".join(lines) + "\n"


def strip_evidence_section(body_md: str) -> str:
    """Remove a previously generated block, if any."""
    return _BLOCK.sub("", body_md)


def attach_evidence(body_md: str, page: PageEvidence) -> str:
    """Body with its evidence section, replacing any block already there.

    Idempotent: `attach_evidence(attach_evidence(b, p), p) == attach_evidence(b, p)`.
    Without that, a vault re-exported over itself grows a second Evidence
    section per cycle and the round-trip check — the only proof the format
    loses nothing — starts failing for a reason that has nothing to do with the
    format.
    """
    base = strip_evidence_section(body_md).rstrip("\n")
    section = render_evidence_section(page)
    if not section:
        return f"{base}\n" if base else ""
    return f"{base}\n\n{section}" if base else section


# --- the json sidecar -------------------------------------------------------


def _citation_json(citation: EvidenceCitation) -> dict[str, Any]:
    return {
        "marker": citation.marker,
        "stance": citation.stance,
        "weight": citation.weight,
        "verbatim": citation.verbatim,
        "source_id": citation.source_id,
        "source_short_id": citation.source_short_id,
        "source_title": citation.source_title,
        "source_url": citation.source_url,
        "chunk_id": citation.chunk_id,
        "quote": citation.quote,
        "char_start": citation.char_start,
        "char_end": citation.char_end,
    }


def _claim_json(claim: ClaimEvidence) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "text": claim.text,
        "confidence": claim.confidence,
        "evidence_tier": claim.evidence_tier,
        "origin": claim.origin,
        "status": claim.status,
        "section_anchor": claim.section_anchor,
        "citations": [_citation_json(c) for c in claim.citations],
    }


def evidence_document(
    pages: Sequence[PageEvidence], *, project_title: str, dialect: str
) -> dict[str, Any]:
    """The sidecar as a dict — the shape, in one place, for both directions.

    `null` is written for an absent field rather than the key being dropped.
    Absent-vs-null is exactly the distinction this data is about: a citation
    with `quote: null` has never been anchored, and a citation missing the key
    is a consumer's guess.
    """
    counts = count_evidence(pages)
    return {
        "aleph_evidence_version": EVIDENCE_FORMAT_VERSION,
        "project_title": project_title,
        "dialect": dialect,
        "claim_count": counts.claims,
        "citation_count": counts.citations,
        "anchored_citation_count": counts.anchored_citations,
        "pages": [
            {
                "slug": page.slug,
                "title": page.title,
                "claims": [_claim_json(claim) for claim in page.claims],
            }
            for page in pages
            if page.claims
        ],
    }


def render_evidence_json(pages: Sequence[PageEvidence], *, project_title: str, dialect: str) -> str:
    """The sidecar's bytes.

    `sort_keys=False` because the key order above is deliberate and stable;
    `ensure_ascii=False` because a quote from a source is somebody's actual
    text and `\\u2014` is not it. A trailing newline so the file is a POSIX
    text file and a `diff` between two exports does not report the last line.
    """
    document = evidence_document(pages, project_title=project_title, dialect=dialect)
    return json.dumps(document, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def evidence_files(
    pages: Sequence[PageEvidence], *, project_title: str, dialect: str
) -> dict[str, str]:
    """The extra bundle entries: the sidecar, or nothing.

    A bundle from a project whose belief layer has never run gets no sidecar
    rather than an empty one. That is a real choice and it has a cost — absent
    then means both "no claims" and "evidence was not requested" — so the route
    reports the counts explicitly in its dry-run response, where the difference
    is visible without opening the zip.
    """
    if not any(page.claims for page in pages):
        return {}
    return {
        EVIDENCE_FILENAME: render_evidence_json(pages, project_title=project_title, dialect=dialect)
    }


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def parse_evidence_json(text: str) -> tuple[Mapping[str, Any], tuple[PageEvidence, ...]]:
    """Read a sidecar back into the view models.

    The import half of the round trip, and the reason the format is checkable:
    `render_evidence_json(parse_evidence_json(x)[1], ...) == x` fails loudly if
    a field is written and not read, which is this codebase's dominant defect
    class applied to a file format.

    A version this module does not know is refused rather than parsed on the
    assumption the keys line up. Returns the raw header alongside the pages so
    a caller can read `project_title` and the counts without re-deriving them.
    """
    raw: Any = json.loads(text)
    if not isinstance(raw, Mapping):
        msg = "evidence sidecar is not a JSON object"
        raise ValueError(msg)
    document = cast("Mapping[str, Any]", raw)
    version = str(document.get("aleph_evidence_version") or "")
    if version != EVIDENCE_FORMAT_VERSION:
        msg = (
            f"evidence sidecar declares version {version!r}; "
            f"this reader knows {EVIDENCE_FORMAT_VERSION!r}"
        )
        raise ValueError(msg)
    pages: list[PageEvidence] = []
    for page_raw in _mappings(document.get("pages")):
        pages.append(
            PageEvidence(
                slug=str(page_raw.get("slug") or ""),
                title=str(page_raw.get("title") or ""),
                claims=tuple(_parse_claim(c) for c in _mappings(page_raw.get("claims"))),
            )
        )
    return document, tuple(pages)


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    """The list-of-objects at a key, with anything else read as absent.

    A sidecar hand-edited into `claims: null` or `claims: "none"` must not
    crash the reader with an AttributeError three frames down; the round trip
    then reports the loss as a diff, which is legible, instead of as a
    traceback naming a private helper.
    """
    if not isinstance(value, list):
        return []
    return [item for item in cast("list[Any]", value) if isinstance(item, Mapping)]


def _parse_claim(raw: Mapping[str, Any]) -> ClaimEvidence:
    return ClaimEvidence(
        claim_id=str(raw.get("claim_id") or ""),
        text=str(raw.get("text") or ""),
        confidence=str(raw.get("confidence") or ""),
        evidence_tier=str(raw.get("evidence_tier") or ""),
        origin=str(raw.get("origin") or ""),
        status=str(raw.get("status") or ""),
        section_anchor=_as_str(raw.get("section_anchor")),
        citations=tuple(_parse_citation(c) for c in _mappings(raw.get("citations"))),
    )


def _parse_citation(raw: Mapping[str, Any]) -> EvidenceCitation:
    start = raw.get("char_start")
    end = raw.get("char_end")
    return EvidenceCitation(
        marker=normalize_marker(str(raw.get("marker") or "")),
        stance=str(raw.get("stance") or ""),
        weight=float(raw.get("weight") or 0.0),
        verbatim=bool(raw.get("verbatim")),
        source_id=_as_str(raw.get("source_id")),
        source_short_id=_as_str(raw.get("source_short_id")),
        source_title=_as_str(raw.get("source_title")),
        source_url=_as_str(raw.get("source_url")),
        chunk_id=_as_str(raw.get("chunk_id")),
        quote=_as_str(raw.get("quote")),
        char_start=None if start is None else int(start),
        char_end=None if end is None else int(end),
    )
