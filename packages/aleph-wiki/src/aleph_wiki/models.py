"""Wiki SQLAlchemy models.

WikiRevision is immutable — guarded by triggers from the Inc 1 migration.
WikiPage carries the current revision pointer. WikiSection enables
sub-page granularity for hand-edit + rejection feedback. Citations bridge
claims to chunks or to source pages.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns
from aleph_rks.models import EMBEDDING_DIM


class WikiPage(CommonColumns, Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_wiki_pages_project_slug"),
        Index("ix_wiki_pages_project_title", "project_id", "title"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(512), nullable=False)
    page_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="topic")
    current_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    is_stub: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="draft")
    last_compiled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Optional key/value metadata the curator may populate (WP-4b). Absent =
    # no infobox. Read by the deterministic HTML compiler to render an infobox
    # table; never a body — markdown stays the only wiki write-format.
    infobox_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # WP-6 trust layer. `volatility` picks the freshness half-life (hot 30d /
    # warm 90d / cold 365d). `verified_at` is the last time a human/agent
    # affirmed the page since its last edit (bumped by the refresh-approve
    # path). `freshness` is the 0-100 curator-computed score
    # (`aleph_wiki.freshness.compute_freshness`).
    volatility: Mapped[str] = mapped_column(String(8), nullable=False, server_default="warm")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    freshness: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    # --- schema governance (aleph_wiki.schema) ------------------------------
    #
    # These mirror the hermes `llm-wiki` frontmatter field-for-field so a vault
    # round-trips through Obsidian without translation. They are stored as
    # columns rather than left in the markdown because every one of them is
    # something the system has to filter, group or lint on, and parsing 251
    # bodies to answer "which pages are contested" is not a query.
    #
    # `page_type` is NOT `page_kind`. `page_kind` records how Aleph produced
    # the page (source ingest, stub minted from a link, synthesis run);
    # `page_type` records what kind of knowledge it holds (concept, entity,
    # comparison, query, hub). A page is routinely both `page_kind="source"`
    # and `page_type="entity"`.
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    page_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Tag order is meaningful to a reader (most specific first), so this is a
    # list, not a set. Membership is constrained by the project's taxonomy at
    # write time, not by the column.
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # Curated "see also", distinct from `wiki_links` — links are extracted from
    # the body, `related` is what the author chose to foreground.
    related: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # How well-supported the page's claims are. NULL means nobody has judged;
    # that is different from `low`, and the lint treats it as a finding rather
    # than assuming the best.
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Set when the page holds unresolved contradictions. `contradictions` names
    # the slugs it conflicts with — a contested page that cannot say what it
    # conflicts with is an assertion a reader cannot check, so the schema
    # rejects the pair.
    contested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    contradictions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")


class WikiSchemaRow(CommonColumns, Base):
    """One project's wiki governance — SCHEMA.md as a row.

    Per-project because a tag taxonomy is a claim about a domain. A project
    studying reactor metallurgy and one studying transformers should not be
    forced to share a vocabulary, and a shared one degrades into the union of
    both, which constrains neither.

    The payload is the JSON form of `aleph_wiki.schema.WikiSchema`; it is
    written whole, never patched field-by-field, so an edit is one auditable
    ledger event rather than a drift of small ones.
    """

    __tablename__ = "wiki_schemas"
    __table_args__ = (UniqueConstraint("project_id", name="uq_wiki_schemas_project"),)

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    payload_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class WikiRevision(Base):
    """Immutable wiki revision. Triggers in the Inc 1 migration reject UPDATE/DELETE."""

    __tablename__ = "wiki_revisions"
    __table_args__ = (UniqueConstraint("page_id", "revision_no", name="uq_wiki_rev_page_no"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    parent_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_message: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)


class WikiSection(Base):
    __tablename__ = "wiki_sections"
    __table_args__ = (
        UniqueConstraint("revision_id", "anchor", name="uq_wiki_sections_rev_anchor"),
        Index("ix_sections_page_rev", "page_id", "revision_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    anchor: Mapped[str] = mapped_column(String(512), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class WikiLink(Base):
    __tablename__ = "wiki_links"
    __table_args__ = (Index("ix_links_src_dst", "src_page_id", "dst_title"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    src_page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    src_revision_id: Mapped[UUID] = mapped_column(nullable=False)
    dst_page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    dst_title: Mapped[str] = mapped_column(String(512), nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class WikiClaim(CommonColumns, Base):
    """A durable belief. See docs/belief-engine.md.

    Identity is ``(project_id, claim_key)`` among live rows, NOT the page
    revision that happened to introduce it. A claim therefore keeps its id, its
    citations, its edges and any human correction across arbitrarily many page
    rewrites — which is the difference between a knowledge layer that
    accumulates and one that is rebuilt from scratch every compile.

    Revision is supersession, never in-place mutation and never DELETE, so the
    history of a belief is walkable without a separate revisions table.
    """

    __tablename__ = "wiki_claims"
    __table_args__ = (
        Index(
            "uq_claims_project_key",
            "project_id",
            "claim_key",
            unique=True,
            postgresql_where=text("superseded_by IS NULL"),
        ),
        Index("ix_claims_live", "project_id", postgresql_where=text("superseded_by IS NULL")),
        Index(
            "ix_claims_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Expression index: no second tsvector column to keep in sync with the
        # text, and therefore no way for the two to disagree.
        Index(
            "ix_claims_text_fts",
            text("to_tsvector('english', text)"),
            postgresql_using="gin",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    #: First-seen provenance only. Nullable because a claim outlives the
    #: revision that introduced it; the name is unchanged so existing readers
    #: keep working.
    revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: The proposition. Capped short by the write path: a 2048-character
    #: "claim" is a paragraph, and a paragraph cannot be contradicted,
    #: retracted or scored. Reasoning belongs in `rationale`.
    text: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: sha256 of the normalized text — the stable identity.
    claim_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("wiki_claims.id", name="fk_claims_superseded_by"), nullable=True
    )
    #: user | agent | research | curator. ``user`` is immutable to agents,
    #: enforced in BeliefService rather than requested in a prompt.
    origin: Mapped[str] = mapped_column(String(16), nullable=False, server_default="agent")
    #: stated | observed | inferred | derived — set by the WRITER, never by the model.
    evidence_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="inferred"
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    support_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    distinct_source_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_surfaced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: RECOMPUTED from evidence, never written by a model. 32 because the
    #: state machine's longest value is "under_investigation" (19).
    confidence: Mapped[str] = mapped_column(String(32), nullable=False, server_default="cited")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class Citation(Base):
    """One anchored piece of evidence for one claim.

    ``stance`` and ``weight`` are named to match
    ``aleph_hypotheses.confidence.EvidenceRow`` exactly, so the existing pure,
    tested confidence engine consumes citations with no adaptation layer.

    ``locator_hash`` makes re-derivation a union rather than a clobber: running
    extraction twice over the same span updates one row instead of inflating
    the evidence for a claim.
    """

    __tablename__ = "citations"
    __table_args__ = (
        Index(
            "uq_citations_claim_locator",
            "claim_id",
            "locator_hash",
            unique=True,
            postgresql_where=text("locator_hash IS NOT NULL"),
        ),
        Index("ix_citations_source", "source_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    claim_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    #: The retraction join key. Was None on every production write path, which
    #: silently voided retraction blast-radius, two freshness dimensions, the
    #: reviewer's source registry and the citation popover.
    source_id: Mapped[UUID | None] = mapped_column(nullable=True)
    chunk_id: Mapped[UUID | None] = mapped_column(nullable=True)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: True only when `aleph_core.grounding.ground` located the quote in the
    #: source. A False here is a citation nobody has checked.
    verbatim: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    stance: Mapped[str] = mapped_column(String(16), nullable=False, server_default="supports")
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    locator_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    source_page_id: Mapped[UUID | None] = mapped_column(nullable=True)
    citation_marker: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourcePage(Base):
    __tablename__ = "source_pages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    extracted_claims_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Alias(CommonColumns, Base):
    __tablename__ = "aliases"
    __table_args__ = (
        UniqueConstraint("project_id", "surface_form", name="uq_aliases_project_surface"),
        Index("ix_aliases_project_canonical", "project_id", "canonical_name"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    surface_form: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_page_id: Mapped[UUID | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")


class HandEditMark(CommonColumns, Base):
    __tablename__ = "hand_edit_marks"
    __table_args__ = (
        Index(
            "ix_handedits_active",
            "page_id",
            postgresql_where=text("cleared_at IS NULL"),
        ),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body_sha256_at_edit: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_by: Mapped[UUID] = mapped_column(nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_by: Mapped[UUID | None] = mapped_column(nullable=True)


class RejectionFeedback(CommonColumns, Base):
    __tablename__ = "rejection_feedbacks"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    concept_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    rejected_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(String(4096), nullable=False)
    rejected_by: Mapped[UUID] = mapped_column(nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    addressed_in_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class WikiIndex(Base):
    __tablename__ = "wiki_index"
    __table_args__ = (
        Index("ix_wiki_index_tsv", "index_tsv", postgresql_using="gin"),
        Index("ix_wiki_index_project_title", "project_id", "title"),
    )

    page_id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    summary: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    # The page body, so the index covers the words a page is actually written
    # in. Weighted below title/summary by the trigger — see the
    # wiki_index_body migration.
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    wikilinks_out_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    page_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_stub: Mapped[bool] = mapped_column(Boolean, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    index_tsv: Mapped[TSVECTOR] = mapped_column(TSVECTOR, nullable=False)


class PageMergeProposal(CommonColumns, Base):
    """A curator-detected near-duplicate: ``source`` should merge into ``target``.

    Human-gated (ApprovalCard) — the curator never merges automatically. On
    approval, ``CuratorService.apply_merge`` redirects inbound links
    source→target, aliases the source title to the target, and soft-deletes the
    source page. Reject leaves both pages. Approval is recorded as a generic
    ``ApprovalDecision`` (``target_kind="page_merge_proposal"``).
    """

    __tablename__ = "page_merge_proposals"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    rationale: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    similarity: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    approval_decision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class ClaimEdge(Base):
    """A typed relation between two beliefs.

    What ``WikiLink(src_page_id, dst_title)`` should have been: revision-free,
    typed, weighted, and between the things that carry meaning rather than
    between the documents that happen to mention them. It is what turns
    retraction from a citation lookup into a graph walk.
    """

    __tablename__ = "claim_edges"
    __table_args__ = (
        Index("ix_claim_edges_src", "src_claim_id", "kind"),
        Index("ix_claim_edges_dst", "dst_claim_id", "kind"),
        Index("uq_claim_edges_triple", "src_claim_id", "dst_claim_id", "kind", unique=True),
        CheckConstraint("src_claim_id <> dst_claim_id", name="ck_claim_edges_no_self_loop"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    src_claim_id: Mapped[UUID] = mapped_column(ForeignKey("wiki_claims.id"), nullable=False)
    dst_claim_id: Mapped[UUID] = mapped_column(ForeignKey("wiki_claims.id"), nullable=False)
    #: supports | contradicts | derived_from | specializes | supersedes
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[UUID] = mapped_column(nullable=False)
    access_scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="project")
