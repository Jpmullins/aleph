"""Wiki governance — SCHEMA.md as data.

A wiki without governance degrades in a specific, predictable way: tags sprawl
into synonyms nobody searches, pages are created for passing mentions, isolated
pages accumulate that nothing links to, and contradictions get silently
overwritten by whichever ingest ran last. The corpus keeps growing and stops
being usable.

The hermes-agent `llm-wiki` skill answers this with a governance file the agent
must read before it writes — a controlled tag taxonomy, a fixed category list,
explicit page thresholds, and a required frontmatter block. That file is what
built `~/wiki/ai-research`, and this module is that file as data.

Why data and not a document: an agent can ignore a document. `validate_page`
runs on the write path, so a page carrying a tag nobody declared is rejected at
commit time rather than discovered during a lint six weeks later. The schema is
per-project and editable because "the tag taxonomy" is a claim about a domain,
and Aleph does not assume every project studies the same one.

The vocabulary here deliberately mirrors the hermes schema field-for-field
(`type`, `category`, `tags`, `related`, `sources`, `confidence`, `contested`,
`contradictions`) so a vault exported from Aleph opens in Obsidian, and a vault
authored in Obsidian imports without a translation layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Self

__all__ = [
    "EXEMPT_TYPES",
    "MIN_OUTBOUND_LINKS",
    "PAGE_SPLIT_LINES",
    "PAGE_STATUSES",
    "REVIEW_QUEUE",
    "STUB_PROMOTION_MENTIONS",
    "STUB_STATUS",
    "WRITING_QUEUE",
    "Category",
    "SchemaViolation",
    "WikiSchema",
    "default_schema",
]

# --- thresholds ------------------------------------------------------------
#
# These three numbers are the whole of the hermes "Page Thresholds" section.
# They live here rather than at their call sites so that a project can raise
# the bar for its own corpus without a code change, and so the numbers a page
# is judged by are the numbers the agent is told about.

#: A page linking to fewer than this many others is invisible: nothing leads to
#: it, so nothing surfaces it. The hermes schema sets 3 for a mature vault (the
#: skill's generic template says 2); Aleph uses 3 because retrieval here is
#: corpus-wide and link structure is what makes a page reachable by walking.
MIN_OUTBOUND_LINKS = 3

#: Past this, a page has stopped being scannable and is really several pages.
PAGE_SPLIT_LINES = 200

#: How many distinct pages must cite a stub before it earns a page of its own.
#:
#: The hermes vault uses 2, and that is right there: a human types `[[link]]`
#: deliberately, so two mentions is two acts of judgement. Aleph extracts links
#: mechanically from compiled prose, where a mention costs nothing — measured
#: over this corpus, a threshold of 2 selects 477 of 600 linked stubs, which
#: measures how common a phrase is, not whether a topic deserves a page.
#:
#: 5 is where the curve flattens on real data (166 pages) and is a starting
#: point, not a law: it is per-project and editable, and a hand-curated vault
#: imported into Aleph should lower it back to 2.
STUB_PROMOTION_MENTIONS = 5

#: The page lifecycle, in order. Two of these are queues for a person and they
#: are NOT the same queue, which is the distinction the original schema lacked:
#:
#: - `stub`     a red link. Nobody wrote it, nobody proposed it. Not a queue.
#: - `planned`  earned a page by being cited enough. A queue for WRITING —
#:              the 🚧 state the hermes index marks planned pages with.
#: - `draft`    has content. A queue for REVIEW.
#: - `approved` reviewed and accepted.
#: - `archived` superseded.
#:
#: Promotion moves a stub to `planned`, never to `draft`. Sending it to `draft`
#: is what put 235 empty pages in front of an approver: "approve this" is not a
#: question you can ask about a page with no content, and a backlog of things
#: to write is supposed to be long in a way an approval queue never is.
PAGE_STATUSES: tuple[str, ...] = ("stub", "planned", "draft", "approved", "archived")

STUB_STATUS = "stub"
#: The statuses that mean a person has something to do, and what they'd be doing.
WRITING_QUEUE = "planned"
REVIEW_QUEUE = "draft"


@dataclass(frozen=True, slots=True)
class Category:
    """One folder in the vault, and the hub page that fronts it."""

    id: str
    title: str
    blurb: str = ""

    @property
    def hub_slug(self) -> str:
        """Hub filename. `_hub` sorts to the top of a folder in Obsidian."""
        return f"{self.id}-hub"


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    """One reason a page does not satisfy the schema.

    Carries `field` and `fix` rather than a bare message because these are shown
    to an agent that has to act on them. "tags: 'transformers' is not in the
    taxonomy" is diagnosable; "invalid page" is not.
    """

    field: str
    message: str
    fix: str = ""

    def __str__(self) -> str:
        return f"{self.field}: {self.message}" + (f" — {self.fix}" if self.fix else "")


# The page types are an epistemic axis: what KIND of knowledge a page holds.
# They are not `WikiPage.page_kind`, which records how Aleph produced the page
# (an ingested source, a stub minted from a link, a synthesis run). A page can
# be `page_kind="source"` and `page_type="entity"` at the same time, and
# conflating the two is how a source page ends up filed as a concept.
# `raw-source` is Layer 1: an immutable ingested document, not a synthesised
# page. It is listed so a vault imports without every raw file reporting a
# violation, but it is exempt from the page rules — nobody writes wikilinks
# into a paper they did not author. Validated against ~/wiki/ai-research: of
# 560 files, the 200 under raw/ carry this type.
PAGE_TYPES: tuple[str, ...] = (
    "concept",
    "entity",
    "comparison",
    "query",
    "hub",
    "raw-source",
)

#: Types exempt from the link, tag and category rules — source material, not
#: wiki pages.
EXEMPT_TYPES: frozenset[str] = frozenset({"raw-source"})

CONFIDENCE_LEVELS: tuple[str, ...] = ("high", "medium", "low")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(slots=True)
class WikiSchema:
    """Per-project governance. One row in `wiki_schemas`, one vault's rules."""

    domain: str
    categories: tuple[Category, ...]
    tags: tuple[str, ...]
    page_types: tuple[str, ...] = PAGE_TYPES
    min_outbound_links: int = MIN_OUTBOUND_LINKS
    page_split_lines: int = PAGE_SPLIT_LINES
    stub_promotion_mentions: int = STUB_PROMOTION_MENTIONS

    # -- lookups ------------------------------------------------------------

    @property
    def category_ids(self) -> frozenset[str]:
        return frozenset(c.id for c in self.categories)

    @property
    def tag_set(self) -> frozenset[str]:
        return frozenset(self.tags)

    def category(self, category_id: str) -> Category | None:
        return next((c for c in self.categories if c.id == category_id), None)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "categories": [
                {"id": c.id, "title": c.title, "blurb": c.blurb} for c in self.categories
            ],
            "tags": list(self.tags),
            "page_types": list(self.page_types),
            "min_outbound_links": self.min_outbound_links,
            "page_split_lines": self.page_split_lines,
            "stub_promotion_mentions": self.stub_promotion_mentions,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Rebuild from stored JSON.

        Tolerant of missing keys so a schema written by an older version still
        loads — the alternative is a project whose wiki stops opening because a
        threshold was added.
        """
        cats = tuple(
            Category(
                id=str(c["id"]),
                title=str(c.get("title") or c["id"].replace("-", " ").title()),
                blurb=str(c.get("blurb") or ""),
            )
            for c in raw.get("categories") or ()
        )
        return cls(
            domain=str(raw.get("domain") or ""),
            categories=cats,
            tags=tuple(str(t) for t in raw.get("tags") or ()),
            page_types=tuple(str(t) for t in raw.get("page_types") or ()) or PAGE_TYPES,
            min_outbound_links=int(raw.get("min_outbound_links") or MIN_OUTBOUND_LINKS),
            page_split_lines=int(raw.get("page_split_lines") or PAGE_SPLIT_LINES),
            stub_promotion_mentions=int(
                raw.get("stub_promotion_mentions") or STUB_PROMOTION_MENTIONS
            ),
        )

    # -- validation ---------------------------------------------------------

    def validate_page(
        self,
        *,
        title: str,
        page_type: str | None,
        category: str | None,
        tags: list[str] | None,
        related: list[str] | None,
        confidence: str | None,
        contested: bool = False,
        contradictions: list[str] | None = None,
        outbound_links: int | None = None,
        body_lines: int | None = None,
        is_stub: bool = False,
    ) -> list[SchemaViolation]:
        """Check one page against the schema.

        Returns every violation rather than raising on the first, because an
        agent fixing a page one round-trip per problem is an agent that gives
        up. A stub is exempt from everything except tag and category legality:
        it has no body to hold links and nobody claimed it was finished. A
        `raw-source` is exempt from the other direction — it is an ingested
        document, and nobody writes wikilinks into a paper they did not author.
        """
        out: list[SchemaViolation] = []
        if page_type in EXEMPT_TYPES:
            return out

        if not title.strip():
            out.append(SchemaViolation("title", "is empty", "every page needs a title"))

        if page_type and page_type not in self.page_types:
            out.append(
                SchemaViolation(
                    "type",
                    f"{page_type!r} is not a page type",
                    f"use one of: {', '.join(self.page_types)}",
                )
            )
        elif not page_type and not is_stub:
            out.append(
                SchemaViolation("type", "is missing", f"set one of: {', '.join(self.page_types)}")
            )

        if category and category not in self.category_ids:
            out.append(
                SchemaViolation(
                    "category",
                    f"{category!r} is not a category in this wiki",
                    "add it to the schema first, or file the page under an existing one",
                )
            )
        elif not category and not is_stub:
            out.append(
                SchemaViolation(
                    "category",
                    "is missing",
                    f"file under one of: {', '.join(sorted(self.category_ids))}",
                )
            )

        # The taxonomy is the point of the taxonomy. A tag invented at write
        # time is a tag nobody will ever search for, because nobody knows it
        # exists — that is how `transformer`, `transformers` and `xformer` end
        # up as three unrelated facets of the same corpus.
        for tag in tags or ():
            if tag not in self.tag_set:
                out.append(
                    SchemaViolation(
                        "tags",
                        f"{tag!r} is not in the taxonomy",
                        "add it to the schema's tag list before using it",
                    )
                )
        if not is_stub and not tags:
            out.append(SchemaViolation("tags", "is empty", "2-5 tags from the taxonomy"))
        elif tags and len(tags) > 8:
            out.append(
                SchemaViolation(
                    "tags",
                    f"{len(tags)} tags is too many to mean anything",
                    "keep 2-5; a page tagged everything is findable by nothing",
                )
            )

        for slug in [*(related or []), *(contradictions or [])]:
            if not _SLUG_RE.match(slug):
                out.append(
                    SchemaViolation(
                        "related",
                        f"{slug!r} is not a slug",
                        "lowercase, hyphens, no spaces or .md",
                    )
                )

        if confidence and confidence not in CONFIDENCE_LEVELS:
            out.append(
                SchemaViolation(
                    "confidence",
                    f"{confidence!r} is not a confidence level",
                    f"use one of: {', '.join(CONFIDENCE_LEVELS)}",
                )
            )

        # `contested` without `contradictions` is the failure the field exists
        # to prevent: a page flagged as disputed that never says what disputes
        # it, which a reader can neither check nor resolve.
        if contested and not contradictions:
            out.append(
                SchemaViolation(
                    "contradictions",
                    "is empty but the page is marked contested",
                    "name the slugs this page conflicts with",
                )
            )

        if not is_stub and outbound_links is not None and outbound_links < self.min_outbound_links:
            out.append(
                SchemaViolation(
                    "wikilinks",
                    f"{outbound_links} outbound links, minimum is {self.min_outbound_links}",
                    "an unlinked page is unreachable by walking the wiki",
                )
            )

        if body_lines is not None and body_lines > self.page_split_lines:
            out.append(
                SchemaViolation(
                    "length",
                    f"{body_lines} lines exceeds {self.page_split_lines}",
                    "split into sub-pages that cross-link to this one",
                )
            )

        return out


# --- the shipped default ---------------------------------------------------
#
# Aleph's first plugin suite is research, so the default schema is the one that
# built `~/wiki/ai-research`. It is a starting point, not an assertion about
# the project — `PUT /wiki/schema` replaces it wholesale, and a project about
# something else should replace it.


def default_schema() -> WikiSchema:
    """The AI/ML research schema, as a new project starts out."""
    return WikiSchema(
        domain=(
            "AI/ML research — technical, graduate-level depth. Architectures, "
            "learning methods, optimization, efficiency, benchmarking and "
            "evaluation, adversarial AI and safety, pre-training, post-training, "
            "new research directions, and the philosophy of AI."
        ),
        categories=(
            Category(
                "foundations", "Foundations", "The load-bearing ideas everything else assumes"
            ),
            Category(
                "architectures", "Architectures", "Structural designs and their inductive biases"
            ),
            Category("learning-methods", "Learning Methods", "How models are taught"),
            Category(
                "optimization", "Optimization", "Optimizers, schedules, and training dynamics"
            ),
            Category(
                "efficiency", "Efficiency", "Doing more with less compute, memory, or latency"
            ),
            Category(
                "benchmarking", "Benchmarking", "Measurement, evaluation, and what scores mean"
            ),
            Category(
                "adversarial", "Adversarial & Safety", "Attacks, robustness, alignment, assurance"
            ),
            Category("pretraining", "Pre-training", "Corpus construction and the pre-training run"),
            Category(
                "posttraining",
                "Post-training",
                "Fine-tuning, preference optimization, distillation",
            ),
            Category("new-directions", "New Directions", "Frontier bets that have not settled"),
            Category(
                "philosophy-of-ai",
                "Philosophy of AI",
                "Meaning, interpretation, epistemology of evaluation",
            ),
            Category("comparisons", "Comparisons", "Side-by-side analyses"),
        ),
        tags=(
            # Core
            "architecture",
            "training",
            "inference",
            "alignment",
            "evaluation",
            "data",
            "optimization",
            "efficiency",
            "safety",
            "scaling",
            # Subfields
            "interpretability",
            "multimodal",
            "reasoning",
            "agents",
            "rl",
            "generative",
            "nlp",
            "vision",
            "audio",
            # Meta
            "comparison",
            "historical",
            "person",
            "lab",
            "dataset",
            "method",
            "benchmark",
            "contested",
            "frontier",
            "open-weights",
            "hub",
            # Philosophy & assurance
            "philosophy",
            "epistemology",
            "meaning",
            "cognition",
            "robustness",
            "assurance",
        ),
    )
