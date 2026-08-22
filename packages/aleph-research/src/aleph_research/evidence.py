"""The evidence pack — what the composer reads, how much of it, and how a
citation is proved.

Before this module the research loop searched for papers, downloaded them,
ingested them, and then composed the report from ``"\n".join(f"c{i}: {title}
— {url}")``. Titles and links. No source text reached the model at any point,
so the prose was written from the model's own recollection of the subject and
the citation numbers were assigned by *position in a list* — ``[c3]`` meant
"the third thing we happened to download", not "this is what that document
says". The live database records the result exactly: 831 citations, 830 of
them with no quote and no chunk anchor.

Three things happen here, and each one is load-bearing:

**Selection.** ``select_cards`` turns ranked retrieval hits into a numbered
pack under a HARD budget. Sending retrieved chunk text instead of a title list
multiplies the compose prompt by one to two orders of magnitude, so "send
everything" is not an option — the budget is stated in characters, enforced,
and reported. The policy is round-robin across the run's sub-questions first
(rank 1 of every question, then rank 2 of every question, …) so a single
question cannot spend the whole budget, then depth cards from the strongest
sources; with a per-source cap throughout, because one long document with
many near-identical passages will otherwise fill the pack and the report is
written from one document's view of the topic while looking corpus-wide.

**Proof.** ``anchor_body`` requires the model to hand back, per marker, a span
it copied out of that card — and grounds it with :func:`aleph_core.grounding.ground`,
which is exact string matching modulo Unicode representation and has no fuzzy
threshold. A quote that is not in its card raises :class:`FabricatedQuote` and
the run fails before anything is committed. This is the only step that can
distinguish "the model read the source" from "the model produced text that
looks like it read the source", and it costs microseconds.

**Anchoring.** A grounded quote yields character offsets into the chunk, and
the chunk carries its own offsets into the normalized document
(``document_chunks.char_start`` — ``packages/aleph-rks/tests/test_chunk_offsets.py``
asserts ``markdown[char_start:char_end] == chunk.text``, which is what makes
the two addable). So a marker resolves to an exact span of an exact document,
and :class:`~aleph_wiki.synthesis_workflow.ResearchSourceRef` carries it.

Everything in this module is pure: no session, no gateway, no clock. The
retrieval half lives in ``research_workflow._gather_evidence`` because it needs
both.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aleph_core.grounding import ground
from aleph_wiki.synthesis_workflow import ResearchClaim, ResearchSourceRef

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

__all__ = [
    "EVIDENCE_CHAR_BUDGET",
    "MAX_CARD_CHARS",
    "MAX_CLAIMS",
    "MAX_EVIDENCE_CARDS",
    "PER_SOURCE_CARD_CAP",
    "AnchoredBody",
    "ChunkRef",
    "EvidenceCard",
    "FabricatedQuote",
    "anchor_body",
    "extract_claims",
    "render_pack",
    "renumber_markers",
    "section_anchor_for",
    "select_cards",
    "split_evidence_block",
]


# ---------------------------------------------------------------------------
# The budget. This is the risk the workstream names, so the numbers are here,
# named, in one place, rather than spread through the prompt builder.
# ---------------------------------------------------------------------------

#: Total characters of source text the pack may carry. ~4 characters per token
#: puts this near 6k tokens; with the 4k `max_tokens` the compose call already
#: asks for, one compose request stays inside a 16k window and well inside the
#: 32k+ every model the gateway serves reports. The old title listing was
#: ~1.2k characters for 15 sources, so this is a 20x increase — deliberately at
#: the bottom of the "one to two orders of magnitude" the risk note warns about,
#: because the cost is paid on every research run.
EVIDENCE_CHAR_BUDGET = 24_000

#: Cards, not characters. A pack of 60 tiny cards is as unreadable to a model as
#: it is to a person, and each card costs a marker the composer has to track.
MAX_EVIDENCE_CARDS = 24

#: Per card. A chunk longer than this is truncated for DISPLAY only — grounding
#: still runs against the whole chunk, so truncation can never turn a real quote
#: into a fabricated one. It can only stop the model quoting text it never saw.
MAX_CARD_CHARS = 1_400

#: A card shorter than this is not worth a marker.
MIN_CARD_CHARS = 200

#: How many cards one source may contribute. Without it, one long document with
#: many near-identical passages fills the pack and the report is written from
#: one document while looking like it read the corpus.
PER_SOURCE_CARD_CAP = 3

#: Floor for a claim. Both apply: a short sentence of real words is a claim
#: ("Two sources agree." is 18 characters), a long run of punctuation is not.
_MIN_CLAIM_CHARS = 12
_MIN_CLAIM_WORDS = 3

#: Claims extracted from one report. Bounds the commit, and a report asserting
#: more than this many distinct cited statements is not a report.
MAX_CLAIMS = 60

_MARKER_RE = re.compile(r"\[(c\d+)\]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

#: Sentence boundary. Copied in shape from `aleph_rks.chunking._SENTENCE_END`,
#: which is private to that package. The input here is model-written Markdown
#: prose, not a parsed PDF, so the abbreviation table that module carries for
#: "Fig. 3" / "et al. 2020" is not needed — but a divergence in the *shape* of
#: the rule would be a silent one, which is why the lineage is written down.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9\[])")

#: The evidence block the composer appends. An HTML comment, so a copy that
#: leaks past the stripper renders as nothing rather than as garbage in a wiki
#: page — and `split_evidence_block` removes every occurrence regardless.
_EVIDENCE_BLOCK_RE = re.compile(r"<!--\s*aleph:evidence\s*(\{.*?\})\s*-->", re.DOTALL)

#: Fallback shape. Models fence JSON by reflex; accepting the fenced form costs
#: one regex and avoids failing a whole run over a formatting habit.
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{(?:[^`]*?)\"quotes\"(?:[^`]*?)\})\s*```")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkRef:
    """One retrieved chunk, with everything needed to cite it.

    ``char_start``/``char_end`` are the CHUNK's offsets into its normalized
    document, not a quote's. A quote's document offsets are these plus the
    offsets `ground` returns within ``text``.
    """

    chunk_id: UUID
    source_id: UUID
    source_short_id: str
    title: str
    url: str | None
    section_path: str | None
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class EvidenceCard:
    """A :class:`ChunkRef` that made it into the pack, with its marker."""

    marker: str
    ref: ChunkRef
    #: What the prompt shows. Equal to ``ref.text`` unless the chunk was longer
    #: than :data:`MAX_CARD_CHARS`.
    display_text: str
    truncated: bool


@dataclass(frozen=True)
class AnchoredBody:
    """The result of proving a composed body against its pack.

    Claims are deliberately NOT extracted here. They carry marker names, and
    the markers are renumbered downstream — extracting them before that would
    leave every claim pointing at a marker that no longer exists. Claims come
    from :func:`extract_claims` over the final body instead.
    """

    body_md: str
    refs_by_marker: dict[str, ResearchSourceRef]
    #: Markers removed because the composer cited a card and then quoted
    #: nothing from it. Reported, never silent: an unquoted citation is exactly
    #: the defect this workstream exists to remove.
    unquoted_markers: list[str]


class FabricatedQuote(RuntimeError):
    """The composer attributed text to a card that the card does not contain.

    Deliberately fatal. Dropping the marker instead would land a report whose
    prose still makes the assertion with the evidence quietly removed, which is
    strictly worse than no report: it looks cited and is not.
    """

    def __init__(self, *, marker: str, quote: str, card: EvidenceCard) -> None:
        self.marker = marker
        self.quote = quote
        self.card = card
        super().__init__(
            f"{marker} quotes text absent from its evidence card "
            f"(source {card.ref.source_short_id}, chunk {card.ref.chunk_id}): "
            f"{quote[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Selection — the budget policy
# ---------------------------------------------------------------------------


#: Appended to a truncated card. Counted against the limit, not added on top of
#: it — a budget that overshoots by four characters per card is not a budget.
_ELLIPSIS = " […]"


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Cut at the last whitespace before ``limit``, marker included. Marks the cut."""
    if len(text) <= limit:
        return text, False
    keep = max(limit - len(_ELLIPSIS), 1)
    cut = text.rfind(" ", 0, keep)
    if cut < keep // 2:
        cut = keep
    return text[:cut].rstrip() + _ELLIPSIS, True


def select_cards(
    *,
    breadth: Sequence[Sequence[ChunkRef]],
    depth: Sequence[ChunkRef] = (),
    budget_chars: int = EVIDENCE_CHAR_BUDGET,
    max_cards: int = MAX_EVIDENCE_CARDS,
    per_source_cap: int = PER_SOURCE_CARD_CAP,
    max_card_chars: int = MAX_CARD_CHARS,
) -> list[EvidenceCard]:
    """Build the numbered pack under the budget. Pure and deterministic.

    ``breadth`` is one ranked list per research sub-question; ``depth`` is the
    extra passages pulled from the strongest sources. The policy, in order:

    1. **Round-robin over the sub-questions.** Rank 1 of every question, then
       rank 2 of every question, and so on. Concatenating the lists instead
       would spend the whole budget on question one whenever question one
       happens to match a lot, and the report would then cover a third of what
       was researched while reporting success.
    2. **Then depth**, in the order the caller ranked it.
    3. **At most ``per_source_cap`` cards per source**, throughout.
    4. **Truncate to whatever budget remains**, and stop entirely once less
       than :data:`MIN_CARD_CHARS` of it is left — a 40-character card is a
       marker the composer cannot quote from, which is worse than one card
       fewer. Truncation is display-only: grounding runs against the whole
       chunk, so it can never turn a real quote into a fabricated one.
    5. Never the same chunk twice, however many questions retrieved it.

    Markers are ``c1..cN`` in output order.
    """
    ordered: list[ChunkRef] = []
    seen_order: set[UUID] = set()
    depth_of_widest = max((len(lst) for lst in breadth), default=0)
    for rank in range(depth_of_widest):
        for lst in breadth:
            if rank < len(lst) and lst[rank].chunk_id not in seen_order:
                seen_order.add(lst[rank].chunk_id)
                ordered.append(lst[rank])
    for ref in depth:
        if ref.chunk_id not in seen_order:
            seen_order.add(ref.chunk_id)
            ordered.append(ref)

    cards: list[EvidenceCard] = []
    per_source: dict[UUID, int] = {}
    remaining = budget_chars
    for ref in ordered:
        if len(cards) >= max_cards or remaining < MIN_CARD_CHARS:
            break
        if per_source.get(ref.source_id, 0) >= per_source_cap:
            continue
        display, truncated = _truncate(ref.text, min(max_card_chars, remaining))
        if not display.strip():
            continue
        per_source[ref.source_id] = per_source.get(ref.source_id, 0) + 1
        remaining -= len(display)
        cards.append(
            EvidenceCard(
                marker=f"c{len(cards) + 1}",
                ref=ref,
                display_text=display,
                truncated=truncated,
            )
        )
    return cards


def render_pack(cards: Sequence[EvidenceCard]) -> str:
    """The prompt block. One stanza per card, marker first."""
    stanzas: list[str] = []
    for card in cards:
        ref = card.ref
        head = f"[{card.marker}] {ref.source_short_id} · {ref.title}"
        if ref.section_path:
            head += f" · {ref.section_path}"
        if ref.url:
            head += f" · {ref.url}"
        stanzas.append(f"{head}\n{card.display_text}")
    return "\n\n".join(stanzas)


# ---------------------------------------------------------------------------
# The composer's response protocol
# ---------------------------------------------------------------------------


def split_evidence_block(content: str) -> tuple[str, dict[str, str]]:
    """Split a compose response into ``(body_md, quotes_by_marker)``.

    The block is taken from the LAST match — a model that restates the format
    mid-answer before emitting the real one is common — and EVERY match is
    stripped from the body, so a leaked copy never reaches a wiki page.

    A missing or unparseable block yields ``{}``, which makes every marker
    unquoted and therefore dropped by :func:`anchor_body`. That is deliberately
    not an exception: the honest failure ("this report anchors nothing") is
    raised once, by the caller, rather than once per formatting variation.
    """
    matches = list(_EVIDENCE_BLOCK_RE.finditer(content))
    body = _EVIDENCE_BLOCK_RE.sub("", content)
    if not matches:
        matches = list(_FENCED_BLOCK_RE.finditer(content))
        body = _FENCED_BLOCK_RE.sub("", content)
    if not matches:
        return content.strip(), {}

    quotes: dict[str, str] = {}
    try:
        parsed = json.loads(matches[-1].group(1))
    except json.JSONDecodeError:
        return body.strip(), {}
    if not isinstance(parsed, dict):
        return body.strip(), {}
    raw = parsed.get("quotes")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    if not isinstance(raw, dict):
        return body.strip(), {}
    for key, value in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if isinstance(key, str) and isinstance(value, str):
            marker = key.strip().strip("[]")
            if marker:
                quotes[marker] = value
    return body.strip(), quotes


def _markers_in(body_md: str) -> list[str]:
    """Distinct ``cN`` markers, in first-appearance order."""
    out: list[str] = []
    for match in _MARKER_RE.finditer(body_md):
        if match.group(1) not in out:
            out.append(match.group(1))
    return out


def _drop_markers(body_md: str, drop: set[str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        return "" if match.group(1) in drop else match.group(0)

    return _MARKER_RE.sub(_sub, body_md)


def anchor_body(
    *,
    body_md: str,
    cards: Sequence[EvidenceCard],
    quotes: Mapping[str, str],
) -> AnchoredBody:
    """Prove every citation in ``body_md``, or refuse.

    For each marker the body uses:

    * no such card → dropped (``sanitize_markers`` normally catches this first);
    * cited but not quoted → dropped, and named in ``unquoted_markers``;
    * quoted but the quote is not in the card → :class:`FabricatedQuote`.

    A surviving marker resolves to a
    :class:`~aleph_wiki.synthesis_workflow.ResearchSourceRef` carrying the chunk
    id, the quote as the SOURCE spells it, and the quote's character span in the
    normalized document.
    """
    by_marker = {card.marker: card for card in cards}
    refs: dict[str, ResearchSourceRef] = {}
    unquoted: list[str] = []
    unknown: list[str] = []

    for marker in _markers_in(body_md):
        card = by_marker.get(marker)
        if card is None:
            unknown.append(marker)
            continue
        quote = (quotes.get(marker) or "").strip()
        if not quote:
            unquoted.append(marker)
            continue
        span = ground(quote, card.ref.text)
        if span is None:
            raise FabricatedQuote(marker=marker, quote=quote, card=card)
        refs[marker] = ResearchSourceRef(
            source_short_id=card.ref.source_short_id,
            title=card.ref.title,
            url=card.ref.url,
            chunk_id=card.ref.chunk_id,
            # The chunk's offset into the document plus the quote's offset into
            # the chunk. Addable because a chunk's text IS the document slice —
            # packages/aleph-rks/tests/test_chunk_offsets.py pins that.
            char_start=card.ref.char_start + span.char_start,
            char_end=card.ref.char_start + span.char_end,
            # The source's spelling, not the model's. They differ whenever
            # normalisation did any work, and the source's is the one that can
            # be checked again later.
            quote=span.matched_text,
        )

    body = _drop_markers(body_md, set(unquoted) | set(unknown))
    return AnchoredBody(body_md=body, refs_by_marker=refs, unquoted_markers=unquoted)


def renumber_markers(
    body_md: str, refs_by_marker: Mapping[str, ResearchSourceRef]
) -> tuple[str, dict[str, ResearchSourceRef]]:
    """Renumber surviving markers to ``c1..cN`` in first-appearance order.

    Run BEFORE ``aleph_scholar.style.style_pass``, which renumbers markers to
    exactly this order itself and knows nothing about the marker→chunk map. The
    old code built the map by list position and then let ``style_pass`` shuffle
    the body's numbers underneath it, so the two disagreed by construction.
    Doing it here first makes ``style_pass``'s renumbering a no-op instead of a
    silent corruption.
    """
    mapping = {old: f"c{i}" for i, old in enumerate(_markers_in(body_md), start=1)}

    def _sub(match: re.Match[str]) -> str:
        new = mapping.get(match.group(1))
        return f"[{new}]" if new else match.group(0)

    renumbered = {mapping[old]: ref for old, ref in refs_by_marker.items() if old in mapping}
    return _MARKER_RE.sub(_sub, body_md), renumbered


# ---------------------------------------------------------------------------
# Claim extraction — deterministic, no model
# ---------------------------------------------------------------------------


def section_anchor_for(heading_text: str) -> str:
    """Slug for a heading, matching ``aleph_wiki.wiki_service._slugify``.

    It has to match: ``wiki_claims.section_anchor`` is joined against the
    anchors ``_split_sections`` derives from the same body at commit time, and a
    claim carrying an anchor no section has is a claim the page cannot show.
    ``tests/e2e/test_research_reads_sources.py`` asserts the two agree, because
    a private function in another package will not tell us when it changes.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", heading_text.lower()).strip("-")
    return slug[:128] or "page"


def _paragraphs(body_md: str) -> list[tuple[str | None, str]]:
    """``(section_anchor, paragraph_text)`` pairs, code fences and headings out.

    List items are separate paragraphs: a bulleted finding is a claim on its
    own, and joining a bullet list into one blob produces a "sentence" that is
    really six.
    """
    out: list[tuple[str | None, str]] = []
    anchor: str | None = None
    buffer: list[str] = []
    fenced = False

    def _flush() -> None:
        if buffer:
            text = " ".join(buffer).strip()
            if text:
                out.append((anchor, text))
            buffer.clear()

    for raw in body_md.splitlines():
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            _flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            _flush()
            anchor = section_anchor_for(heading.group(2))
            continue
        if not line.strip():
            _flush()
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is not None:
            _flush()
            buffer.append(line[bullet.end() :])
            continue
        buffer.append(line.strip())
    _flush()
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def extract_claims(
    body_md: str,
    *,
    known_markers: Sequence[str] | set[str],
    max_claims: int = MAX_CLAIMS,
) -> list[ResearchClaim]:
    """Every cited sentence, as a claim carrying the markers that support it.

    ``build_report`` used to pass an empty claim list — the loop wrote the citation
    markers into prose and then threw away the statements they were attached
    to, so the synthesis commit had nothing to write claims or citations from
    and the research path produced zero of both. (Checked on the live stack:
    seven succeeded research runs, seven synthesis proposals, zero citations.)

    No model: a claim is a sentence of the report that carries at least one
    marker whose card survived grounding. The sentence is stored WITHOUT its
    markers — the markers are the citation edge, not part of the assertion.
    """
    known = set(known_markers)
    claims: list[ResearchClaim] = []
    for anchor, paragraph in _paragraphs(body_md):
        for sentence in _sentences(paragraph):
            markers = [m for m in _markers_in(sentence) if m in known]
            if not markers:
                continue
            text = _MARKER_RE.sub("", sentence)
            text = re.sub(r"\s+([.,;:!?])", r"\1", text)
            text = re.sub(r"\s{2,}", " ", text).strip()
            # A fragment that is only a citation ("[c1].") asserts nothing.
            # Three words is the floor rather than a character count, because
            # "Two sources agree." is a real claim and 18 characters long.
            if len(text) < _MIN_CLAIM_CHARS or len(text.split()) < _MIN_CLAIM_WORDS:
                continue
            claims.append(
                ResearchClaim(
                    text=text[:2048],
                    citation_markers=markers,
                    section_anchor=anchor,
                )
            )
            if len(claims) >= max_claims:
                return claims
    return claims
