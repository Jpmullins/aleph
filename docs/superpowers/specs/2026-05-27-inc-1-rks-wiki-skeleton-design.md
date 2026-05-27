# Increment 1 — RKS + Intra-Source Retrieval + Wiki Skeleton

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md` (top-level, §13)
**Depends on:** Inc 0 (`2026-05-27-inc-0-foundations-design.md`)
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 1.1 Scope

Increment 1 establishes the **Raw Knowledge Store (RKS)** and the **wiki skeleton**. After this increment, an analyst can upload a PDF (or other supported document) and the system creates a `SourcePage` plus initial topic pages, all backed by hashed asset storage, normalized markdown, embedded chunks for intra-source descent, and a working `WikiIndex`. **No assistant chat yet** — that's Increment 2.

This is the increment where the wiki-first retrieval *substrate* is real, even though querying it via chat doesn't land until Inc 2. Every entity the rest of the system relies on for retrieval exists at the end of this increment.

### In scope

- RKS entities: `Source`, `SourceVersion`, `SourceAsset`, `NormalizedDocument`, `DocumentChunk`, `RetrievalIndexRecord`
- `Connector` + `ConnectorBinding` (typed plugin registry; only the Upload connector is registered)
- Upload connector — file ingest path (PDF, MD, TXT, DOCX, HTML, EPUB)
- Normalization worker (markdown produced by a layered parser pipeline)
- Chunking + embedding worker (intra-source pgvector index)
- Wiki entities: `WikiPage`, `WikiRevision` (immutable), `WikiSection`, `WikiLink`, `WikiClaim`, `Citation`, `SourcePage`, `Alias`, `HandEditMark`, `RejectionFeedback`, `WikiIndex`
- Wiki agent (LangGraph workflow) — ingest path that turns a normalized document into wiki content
- Wiki service (immutable revisions + hand-edit detection + rejection-feedback ingestion)
- HTTP API for sources, normalized docs, chunks, wiki pages, revisions, claims, citations, aliases, hand-edits
- UI shell: Sources tab and Wiki tab populated with placeholder rendering (no A2UI yet — lands in Inc 4)
- Tests, docs, runbook updates

### Explicitly out of scope

- Assistant chat → Increment 2
- AIQ research → Increment 3
- Connectors other than Upload → Increment 3
- ConnectorCredential (encrypted store) → Increment 3 (no other connector needs credentials yet)
- A2UI surfaces → Increment 4
- Reviewers + approval workflow → Increment 5 (`RejectionFeedback` *consumption* by wiki agent lands now; the user-facing rejection flow lands then)
- Hypotheses, Notebook → Increment 5
- Datasets, charts → Increment 6
- Builder, artifacts → Increment 7

### What downstream increments rely on

- **Inc 2** retrieval router queries `WikiIndex`, loads `WikiPage`s, traverses `WikiLink`s, descends into `DocumentChunk`s within a single `Source`.
- **Inc 3** AIQ writes new `Source`s and triggers the same ingest path; `--synthesize` writes new `WikiPage`s through the same `wiki_service.commit_revision()`.
- **Inc 4** A2UI surfaces render `WikiPage`s, `SourcePage`s, etc. via `WikiSurface`.
- **Inc 5** Reviewers operate on `WikiClaim`s, `Citation`s, `WikiLink`s, `HandEditMark`s; rejection feedback writes `RejectionFeedback` rows that the wiki agent reads on next compile (the consumption path is already wired here).

---

## 1.2 Repository changes

New packages and worker modules:

```
packages/
├── aleph-rks/                          # RKS domain + services
│   └── src/aleph_rks/
│       ├── __init__.py
│       ├── models.py                   # Source, SourceVersion, SourceAsset, NormalizedDocument, DocumentChunk, RetrievalIndexRecord, Connector, ConnectorBinding
│       ├── source_service.py           # business logic
│       ├── asset_store.py              # MinIO/S3 wrapper for source assets
│       ├── normalization.py            # Normalizer protocol + impls
│       ├── chunking.py                 # sentence-aware chunker with overlap
│       ├── embedding.py                # batched embedding via LiteLLM
│       └── retrieval.py                # intra-source descent (queried in Inc 2)
├── aleph-wiki/                         # Wiki domain + services + agent
│   └── src/aleph_wiki/
│       ├── __init__.py
│       ├── models.py                   # WikiPage, WikiRevision, WikiSection, WikiLink, WikiClaim, Citation, SourcePage, Alias, HandEditMark, RejectionFeedback, WikiIndex
│       ├── wiki_service.py             # commit_revision, fetch helpers
│       ├── index_service.py            # WikiIndex maintenance
│       ├── alias_service.py            # alias normalization, wikilink repair
│       ├── handedit_service.py         # mark/clear, diff-vs-compile
│       ├── feedback_service.py         # read/write RejectionFeedback
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── workflow.py             # LangGraph DAG (the wiki ingest workflow)
│       │   ├── nodes/
│       │   │   ├── concept_extraction.py
│       │   │   ├── alias_extraction.py
│       │   │   ├── source_page_compose.py
│       │   │   ├── topic_page_stubs.py
│       │   │   ├── wikilink_resolve.py
│       │   │   ├── wiki_index_update.py
│       │   │   └── commit_revision.py
│       │   └── prompts/
│       │       ├── concept_extraction.md
│       │       ├── alias_extraction.md
│       │       ├── source_page_compose.md
│       │       └── topic_page_stub.md
│       └── connectors/
│           ├── __init__.py
│           ├── base.py                 # Connector / ConnectorBase Protocol
│           └── upload.py               # the Upload connector
```

New worker jobs:

```
apps/workers/src/aleph_workers/jobs/
├── normalize.py                        # Source → NormalizedDocument
├── chunk_embed.py                      # NormalizedDocument → DocumentChunk[]
└── wiki_ingest.py                      # NormalizedDocument → wiki commit (runs the LangGraph workflow)
```

New API routes:

```
apps/api/src/aleph_api/routes/
├── sources.py                          # CRUD + upload + status
├── normalized.py                       # GET normalized markdown
├── chunks.py                           # GET chunks for a source (intra-source retrieval used in Inc 2)
├── wiki.py                             # pages, revisions, claims, citations, links
├── source_pages.py                     # special view of SourcePages
├── aliases.py                          # list, add, repair
├── handedits.py                        # mark / clear
└── feedback.py                         # write RejectionFeedback
```

New frontend additions:

```
apps/web/src/
├── routes/project/$id/
│   ├── sources.tsx                     # Sources list under the workspace
│   ├── source.$sourceId.tsx            # Source detail with normalized preview
│   ├── wiki.tsx                        # Wiki index/landing
│   └── wiki.$pageId.tsx                # Wiki page view (plain Markdown for Inc 1)
└── components/
    ├── SourceUploadModal.tsx
    ├── SourceList.tsx
    ├── NormalizedPreview.tsx
    ├── WikiIndexView.tsx
    ├── WikiPageView.tsx
    └── WikilinkChip.tsx                # `[[wikilink]]` renderer used inside MD
```

New documentation:

- `docs/domain/rks.md`
- `docs/domain/wiki.md`
- `docs/domain/claims-and-provenance.md`
- `docs/pipelines/normalization.md`
- `docs/pipelines/chunking-and-embedding.md`
- `docs/agents/wiki-agent.md`
- `docs/wiki/hand-edits.md`
- `docs/wiki/rejection-feedback.md`
- `docs/wiki/aliases.md`
- `docs/connectors/upload.md`

New dependencies (verify latest at install time per Inc 0 §15.6):

- Python: `pypdf`, `pdfminer.six` (PDF fallback), `python-docx`, `readability-lxml`, `lxml`, `beautifulsoup4`, `ebooklib`, `markdown-it-py`, `tiktoken`, `numpy`, `scipy` (for cosine sims if needed), `langgraph` (Inc 0 already declared in the workspace), `pgvector` Python package
- JS: nothing new in Inc 1 beyond Inc 0

---

## 1.3 Domain model — concrete schemas

All new tables use the Inc 0 `CommonColumns` mixin (UUIDv7 PK, `created_at/updated_at`, `created_by`, `access_scope`, `trace_id?`, `ledger_event_id?`).

### 1.3.1 RKS

```python
# packages/aleph-rks/src/aleph_rks/models.py

class Connector(CommonColumns, Base):
    """A typed source-kind plugin. Registered at app boot via aleph_rks.connectors discovery."""
    __tablename__ = "connectors"
    kind: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # upload | web_search | paper_search | feed | structured_api | model_repo
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    output_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # document | dataset_rows
    requires_auth: Mapped[bool] = mapped_column(nullable=False, default=False)
    metadata_schema_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # JSON-Schema-shaped: describes per-source metadata this connector emits
    enabled_by_default: Mapped[bool] = mapped_column(nullable=False, default=False)

class ConnectorBinding(CommonColumns, Base):
    """Per-project allowlist for a connector. ConnectorCredential lands in Inc 3."""
    __tablename__ = "connector_bindings"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    connector_id: Mapped[UUID] = mapped_column(nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    config_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # connector-specific per-project config (e.g. max results, search depth)

    __table_args__ = (UniqueConstraint("project_id", "connector_id"),)

class Source(CommonColumns, Base):
    __tablename__ = "sources"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    connector_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # connector-stable identifier; for upload it's the original filename + uploader user_id
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_metadata_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # validated against Connector.metadata_schema_jsonb
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    # human-stable id used in [[Source:<short_id>]] wikilinks; e.g. "S0042"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ingested")
    # ingested | normalizing | normalized | chunking | indexed | failed
    failure_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("ix_sources_project_status", "project_id", "status"),
        Index("ix_sources_short_id", "short_id"),
    )

class SourceVersion(CommonColumns, Base):
    """Each refetch / re-upload of the same source creates a new version. Old versions preserved."""
    __tablename__ = "source_versions"
    source_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(nullable=False)
    asset_id: Mapped[UUID] = mapped_column(nullable=False)
    # FK source_assets.id
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_document_id: Mapped[UUID | None] = mapped_column(nullable=True)

    __table_args__ = (UniqueConstraint("source_id", "version_no"),)

class SourceAsset(CommonColumns, Base):
    """The raw bytes. Stored in MinIO/S3; this row carries the pointer + provenance."""
    __tablename__ = "source_assets"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    # s3://bucket/projects/{project_id}/sources/{source_id}/{sha256}.{ext}
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

class NormalizedDocument(CommonColumns, Base):
    __tablename__ = "normalized_documents"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_version_id: Mapped[UUID] = mapped_column(nullable=False)
    markdown_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    # s3://bucket/projects/{project_id}/normalized/{source_id}/{version_no}.md
    parser: Mapped[str] = mapped_column(String(64), nullable=False)
    # pypdf | pdfminer | python-docx | readability-lxml | passthrough | markitdown | ...
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(nullable=False)
    token_count: Mapped[int] = mapped_column(nullable=False)
    structure_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # outline-ish: headings, tables-extracted-count, figures-extracted-count
    quality_flags_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # e.g. ["short-content", "ocr-required", "tables-not-extracted"]

class DocumentChunk(Base):
    """Embedded retrieval unit. USED FOR INTRA-SOURCE DESCENT ONLY (§3.3 top-level)."""
    __tablename__ = "document_chunks"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    normalized_document_id: Mapped[UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_tsv: Mapped[TSVECTOR] = mapped_column(TSVECTOR, nullable=False)
    # generated column from text using simple english config; index below
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    # dimension is the embedding model dimension; cohere-embed-* is 1024
    section_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # e.g. "Methods > Sample Selection"
    char_start: Mapped[int] = mapped_column(nullable=False)
    char_end: Mapped[int] = mapped_column(nullable=False)
    token_count: Mapped[int] = mapped_column(nullable=False)
    embedder_model: Mapped[str] = mapped_column(String(128), nullable=False)
    # gateway-side embedding model name used; if profile changes, re-embed runs

    __table_args__ = (
        Index("ix_chunks_source_ord", "source_id", "ordinal"),
        Index("ix_chunks_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        Index("ix_chunks_text_fts", "text_tsv", postgresql_using="gin"),
    )

class RetrievalIndexRecord(CommonColumns, Base):
    """Per-source index pointer. Captures embedder model + index version
    so a profile change triggers re-embedding cleanly."""
    __tablename__ = "retrieval_index_records"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    embedder_model: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_count: Mapped[int] = mapped_column(nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

### 1.3.2 Wiki

```python
# packages/aleph-wiki/src/aleph_wiki/models.py

class WikiPage(CommonColumns, Base):
    __tablename__ = "wiki_pages"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(512), nullable=False)
    # url-safe; unique per project
    page_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="topic")
    # topic | source | synthesis | stub
    # source-kind pages are SourcePages; backed by a Source
    current_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    is_stub: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    # draft | approved | archived
    last_compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("project_id", "slug"),
        Index("ix_wiki_pages_project_title", "project_id", "title"),
    )

class WikiRevision(Base):
    """Immutable. No update. No delete (except via project deletion cascade)."""
    __tablename__ = "wiki_revisions"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # ~200 token agent-generated summary; used by WikiIndex page-selector
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # user | aleph_agent | aiq_agent
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    parent_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_message: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("page_id", "revision_no"),)

# Same immutability triggers as ActionLedgerEvent (Inc 0 §0.4.4).

class WikiSection(Base):
    """Sub-page granularity for hand-edit + rejection feedback. A section is a
    contiguous range of the current revision's body_md identified by stable
    section_anchor (heading slug)."""
    __tablename__ = "wiki_sections"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    anchor: Mapped[str] = mapped_column(String(512), nullable=False)
    # heading slug, e.g. "transformer-capacity"
    char_start: Mapped[int] = mapped_column(nullable=False)
    char_end: Mapped[int] = mapped_column(nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("revision_id", "anchor"),
        Index("ix_sections_page_rev", "page_id", "revision_id"),
    )

class WikiLink(Base):
    """A [[wikilink]] from one page to another. Rebuilt on every commit."""
    __tablename__ = "wiki_links"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    src_page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    src_revision_id: Mapped[UUID] = mapped_column(nullable=False)
    dst_page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    # null = unresolved link (alias points nowhere yet)
    dst_title: Mapped[str] = mapped_column(String(512), nullable=False)
    # the surface form as written in the source page
    occurrences: Mapped[int] = mapped_column(nullable=False, default=1)

    __table_args__ = (Index("ix_links_src_dst", "src_page_id", "dst_title"),)

class WikiClaim(CommonColumns, Base):
    __tablename__ = "wiki_claims"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text: Mapped[str] = mapped_column(String(2048), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="cited")
    # well-supported | weakly-supported | contested | uncited
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # active | superseded | rejected

class Citation(Base):
    """Claim → DocumentChunk[] | SourcePage edge. Either chunk_ids or source_page_id is set."""
    __tablename__ = "citations"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    claim_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # array of UUIDs from document_chunks
    source_page_id: Mapped[UUID | None] = mapped_column(nullable=True)
    citation_marker: Mapped[str] = mapped_column(String(16), nullable=False)
    # "[c12]" as it appears in body_md
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

class SourcePage(Base):
    """Bridge entity. One WikiPage per Source. Created by the wiki agent on ingest.

    SourcePage is NOT a duplicate of Source — it's the wiki-side view: structured
    metadata + extracted claims + link to asset + intra-source chunk index handle.
    The WikiPage row carries the prose; this row carries the bridge metadata.
    """
    __tablename__ = "source_pages"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    extracted_claims_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class Alias(CommonColumns, Base):
    __tablename__ = "aliases"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    surface_form: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_page_id: Mapped[UUID | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(nullable=False, default=1.0)

    __table_args__ = (
        UniqueConstraint("project_id", "surface_form"),
        Index("ix_aliases_project_canonical", "project_id", "canonical_name"),
    )

class HandEditMark(CommonColumns, Base):
    """Records analyst-edited region. The wiki agent must NOT regenerate this section."""
    __tablename__ = "hand_edit_marks"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # null = whole page
    body_sha256_at_edit: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_by: Mapped[UUID] = mapped_column(nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_by: Mapped[UUID | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("ix_handedits_active", "page_id",
              postgresql_where=text("cleared_at IS NULL")),
    )

class RejectionFeedback(CommonColumns, Base):
    """Analyst rejection of a wiki draft. Consumed by next compile of same concept.

    Inc 1 implements the model + the wiki agent's read-on-compile behavior.
    The rejection is *written* by the reviewer/approval UI in Inc 5; until then,
    rows can be inserted manually by tests or admin endpoints.
    """
    __tablename__ = "rejection_feedbacks"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    concept_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    # the canonical name; rejections key by concept, not page id
    rejected_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(String(4096), nullable=False)
    rejected_by: Mapped[UUID] = mapped_column(nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    addressed_in_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # set when a subsequent compile produces a revision approved against this feedback

class WikiIndex(Base):
    """Denormalized retrieval index. One row per WikiPage. Rebuilt incrementally.

    Decision (locked, top-level §16.3): Postgres-only denormalized table.
    FTS via GIN(tsvector); page-selector queries this table.
    """
    __tablename__ = "wiki_index"
    page_id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    wikilinks_out_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # list of {dst_title, dst_page_id?, occurrences}
    page_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_stub: Mapped[bool] = mapped_column(nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    index_tsv: Mapped[TSVECTOR] = mapped_column(TSVECTOR, nullable=False)
    # generated from title + aliases + summary

    __table_args__ = (
        Index("ix_wiki_index_tsv", "index_tsv", postgresql_using="gin"),
        Index("ix_wiki_index_project_title", "project_id", "title"),
    )
```

### 1.3.3 Migration

`apps/api/alembic/versions/<timestamp>_inc1_rks_wiki.py`:

- Creates every table above
- Creates HNSW vector index on `document_chunks.embedding`
- Creates GIN tsvector indexes on `document_chunks.text_tsv` and `wiki_index.index_tsv`
- Creates immutability triggers on `wiki_revisions` (same shape as `action_ledger_events` triggers in Inc 0)
- Seeds the `Connector` row for the Upload connector

---

## 1.4 Asset storage layout

`SourceAsset.storage_uri` follows `s3://<bucket>/projects/{project_id}/sources/{source_id}/{sha256}.{ext}`. Normalized markdown lives at `s3://<bucket>/projects/{project_id}/normalized/{source_id}/{version_no}.md`.

Both directly hashable for tamper detection. `asset_store.fetch` verifies SHA-256 on read.

---

## 1.5 Connector framework + Upload connector

```python
# packages/aleph-wiki/src/aleph_wiki/connectors/base.py

class ConnectorBase(Protocol):
    kind: ClassVar[str]
    output_kind: ClassVar[Literal["document", "dataset_rows"]]
    requires_auth: ClassVar[bool]
    metadata_schema: ClassVar[type[BaseModel]]

    async def search(self, query: SearchQuery, project: ProjectScope) -> list[ConnectorResult]:
        """For non-search connectors (upload, RSS push), raises NotSupported."""

    async def fetch(self, result: ConnectorResult, project: ProjectScope) -> RawPayload:
        """Returns the raw bytes + claimed metadata."""

    async def normalize(self, payload: RawPayload) -> NormalizedDocument | DatasetRows:
        """For document connectors, returns markdown + structural metadata.
        For dataset_rows connectors, returns typed rows."""
```

The Upload connector is the only one registered in Inc 1:

```python
# packages/aleph-wiki/src/aleph_wiki/connectors/upload.py
class UploadConnector:
    kind = "upload"
    output_kind = "document"
    requires_auth = False
    metadata_schema = UploadMetadata  # filename, mime_type, uploader_id, original_size

    async def search(self, *args, **kwargs):
        raise NotSupported("upload does not support search")

    async def fetch(self, ...): ...  # for upload, "fetch" is read-from-MinIO

    async def normalize(self, ...): ...
```

In Inc 1 the connector runs in-process inside `aleph-workers` (not via AIQ — AIQ doesn't exist yet). In Inc 3 it gets re-registered as a `nat` function so it appears in AIQ's `data_source_registry` like the rest.

---

## 1.6 Normalization

`packages/aleph-rks/src/aleph_rks/normalization.py` defines a `Normalizer` protocol and per-MIME-type implementations:

| MIME | Primary | Fallback |
|---|---|---|
| `application/pdf` | `pypdf` | `pdfminer.six` |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `python-docx` → markdown | pandoc via `pypandoc` |
| `text/html` | `readability-lxml` + `beautifulsoup4` | `trafilatura` |
| `text/markdown`, `text/plain` | passthrough | none |
| `application/epub+zip` | `ebooklib` → markdown | none |

The normalizer outputs:

- a Markdown string (canonicalized; headings, paragraphs, lists; tables converted to markdown tables where possible)
- a `structure_jsonb` (heading outline, table count, figure count)
- `quality_flags` (e.g. `ocr-required` if PDF text extraction yields <100 chars per page, `tables-not-extracted` if tables detected but couldn't be parsed)

**`parser_version`** is set to `<library>@<library_version>` (e.g. `pypdf@5.5.0`). When parser_version changes for a previously-normalized source, the source becomes re-normalizable — but **re-normalization is opt-in, not automatic.** Triggered by a dedicated admin endpoint that creates a new `SourceVersion`.

Failure handling: per Inc 0 §0.15, the worker marks the `Source.status = "failed"` with `failure_reason`. No silent failure. UI surfaces failed sources clearly.

---

## 1.7 Chunking + embedding

```python
# packages/aleph-rks/src/aleph_rks/chunking.py

def chunk(
    markdown: str,
    *,
    target_tokens: int = 512,
    overlap_tokens: int = 64,
    encoder_name: str = "cl100k_base",
) -> list[Chunk]:
    """Sentence-boundary-aware chunking with overlap.

    Algorithm:
      1. Split on Markdown structural boundaries: ATX/Setext headings, lists, code
         fences. Preserve heading anchors for section_path.
      2. Within each structural region, segment by sentence using a deterministic
         splitter (regex; not LLM-driven).
      3. Greedy pack sentences into chunks up to target_tokens. Carry overlap_tokens
         from the previous chunk forward when starting a new chunk.
      4. Code/table blocks are kept whole if <= 2 * target_tokens; oversized blocks
         are split on row/line boundaries.
    """
```

Tokens counted via `tiktoken.get_encoding("cl100k_base")` — chosen as a stable, deterministic counter independent of the gateway's embedding model. Chunks just need to fit; exact tokenizer match is not required.

Embedding worker batches up to 96 chunks per gateway call (cohere v3/v4 batch limit; configurable). Calls `LiteLLMClient.embed(..., capability="embedding", purpose="rks.embed")`. Cost is ledgered like any other LLM call.

`embedder_model` is recorded on each `DocumentChunk` row. If a project's `ModelProfile.embedding` binding changes, the `RetrievalIndexRecord` becomes stale; a re-embed job is triggered for that project's chunks (Inc 2 acceptance includes this triggered re-embed working).

---

## 1.8 Wiki agent (LangGraph workflow)

`packages/aleph-wiki/src/aleph_wiki/agent/workflow.py` defines the LangGraph DAG that runs when a `NormalizedDocument` is committed.

### Workflow state

```python
class WikiIngestState(TypedDict):
    agent_run_id: UUID
    project_id: UUID
    source_id: UUID
    source_short_id: str
    normalized_document_id: UUID
    profile: ModelProfile
    rejection_context: list[RejectionFeedback]  # loaded at start

    # progressive results
    concepts: list[ExtractedConcept] | None
    aliases: list[ExtractedAlias] | None
    source_page_draft: WikiPageDraft | None
    topic_page_drafts: list[WikiPageDraft] | None
    wikilinks_resolved: list[WikiLinkDraft] | None
    committed_revision_ids: list[UUID] | None
```

### Nodes (executed in order, each one a LangGraph node)

1. **`concept_extraction`** — single LLM call (`extraction` capability) over the normalized markdown. Output: list of `ExtractedConcept(canonical_name, surface_forms[], confidence, salience)`. Prompt at `prompts/concept_extraction.md`. Schema-constrained Pydantic output.
2. **`alias_extraction`** — single LLM call (`extraction`) using the concept list as input. Output: list of `ExtractedAlias(surface_form, canonical_name, confidence)`. Persists `Alias` rows.
3. **`source_page_compose`** — single LLM call (`synthesis`) producing the `SourcePage` body: structured metadata block + extracted-claims list + back-link to asset + cross-references to concepts. Prompt at `prompts/source_page_compose.md`.
4. **`topic_page_stubs`** — for each new canonical concept, one LLM call (`extraction`/light-synthesis). If a topic page already exists: extend it (subject to `HandEditMark` respect). If new: create a stub page with: page title, two-paragraph definition derived from this source's mentions, list of sources referencing the concept. Prompts at `prompts/topic_page_stub.md`. **The wiki doesn't try to deep-synthesize in Inc 1** — that's `--synthesize` territory (Inc 3). Inc 1 ensures coverage minimum: every concept has a stub.
5. **`wikilink_resolve`** — deterministic. For each page draft, scan for `[[<text>]]` markers. Use `Alias` table to resolve to canonical names. If alias points to an existing page, set `WikiLink.dst_page_id`. If not, leave `dst_page_id` null (unresolved — the next concept extraction may resolve it). Insertion of `[[Source:<short_id>]]` markers happens here based on what `Citation` rows exist for the page's claims.
6. **`wiki_index_update`** — deterministic. For every affected `WikiPage`, recompute the `WikiIndex` row from current revision. Update `wikilinks_out_jsonb`.
7. **`commit_revision`** — for each `WikiPageDraft`, call `wiki_service.commit_revision()` (§1.9). One ledger event per page committed. One trace span per `commit_revision` call.

### Hand-edit + rejection feedback wiring

- **Hand-edit:** nodes 3, 4 (compose, stubs) accept the existing page's `HandEditMark` set. When emitting a new revision, any `HandEditMark`-protected section is carried over verbatim from the prior revision. The LLM sees the existing protected section text in its input as "DO NOT MODIFY" context.
- **Rejection feedback:** `rejection_context` is loaded at workflow start, filtering `RejectionFeedback` rows by `(project_id, concept_name)` where `addressed_in_revision_id IS NULL`. Each compile prompt includes the rejection reasons as constraints. When `commit_revision` writes the new revision, the agent updates the rejection feedback row's `addressed_in_revision_id`.

### LangGraph configuration

- Uses `StateGraph[WikiIngestState]`
- Edges are linear (no branching in Inc 1 — the workflow is deterministic in shape)
- Each node wraps its work in an OTEL span with attributes `aleph.node`, `aleph.agent_kind="wiki"`, `aleph.project_id`, `aleph.source_id`
- An `AgentRun` row is created at workflow start; an `AgentEvent` for `phase_started` and `phase_completed` per node
- Failure of any node: `AgentRun.status = "failed"`, `error_text` populated, ledger event emitted, source status updated to reflect partial progress (`status = "wiki_partial"` if at least source_page was committed, else `"wiki_failed"`)

---

## 1.9 Wiki service

```python
# packages/aleph-wiki/src/aleph_wiki/wiki_service.py

class WikiService:
    async def commit_revision(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        page_id: UUID | None,  # None = new page; provide title/slug instead
        title: str | None,
        slug: str | None,
        page_kind: Literal["topic", "source", "synthesis", "stub"],
        body_md: str,
        summary: str,
        claims: list[ClaimDraft],
        citations: list[CitationDraft],
        wikilinks: list[WikiLinkDraft],
        commit_message: str,
        respect_hand_edits: bool = True,
    ) -> CommitResult:
        """Atomic. In one transaction:
          1. Lock the WikiPage row (or create if new).
          2. Apply HandEditMark protection: for each protected section in the prior
             revision, splice the protected text back into body_md (preserving order
             by anchor).
          3. Compute body_sha256 of the protected-merged body.
          4. If body_sha256 unchanged from current revision → no-op (return existing
             revision_id, log a "skipped (idempotent)" trace event).
          5. Insert WikiRevision (revision_no = prior + 1, parent_revision_id).
          6. Insert WikiSection rows for the new revision.
          7. Replace WikiLink rows for src_revision_id.
          8. Insert WikiClaim + Citation rows.
          9. Update WikiPage.current_revision_id, last_compiled_at, is_stub.
         10. Update WikiIndex row (via IndexService.refresh_page).
         11. Append ActionLedgerEvent (action_kind="wiki.revision.commit").
         12. Return CommitResult with all new IDs.
        """

    async def get_page(self, project_id: UUID, page_id: UUID, revision_id: UUID | None = None) -> WikiPageRead: ...
    async def list_pages(self, project_id: UUID, *, kind: str | None = None) -> list[WikiPageSummary]: ...
    async def page_by_slug(self, project_id: UUID, slug: str) -> WikiPageRead | None: ...
    async def page_by_source(self, project_id: UUID, source_id: UUID) -> WikiPageRead | None: ...
```

`HandEditMarkService` provides:

```python
async def mark_section(principal, project_id, page_id, section_anchor) -> HandEditMark: ...
async def clear_section(principal, project_id, page_id, section_anchor) -> None: ...
async def list_active_for_page(project_id, page_id) -> list[HandEditMark]: ...
```

`AliasService`:

```python
async def upsert(project_id, surface_form, canonical_name, *, page_id, confidence) -> Alias: ...
async def resolve_link(project_id, surface_form) -> AliasResolution: ...
# returns (canonical_name, page_id?) -- None if unresolved
async def repair_broken_links(project_id) -> int:
    """Iterate WikiLink rows with null dst_page_id; try to resolve via aliases.
    Idempotent. Returns count repaired. Called from wiki_index_update node."""
```

`IndexService`:

```python
async def refresh_page(project_id, page_id) -> None:
    """Rebuild the WikiIndex row for one page. Reads the current revision's body
    + claims to compute summary + aliases + wikilinks_out."""

async def select_pages(project_id, query: str, *, top_k: int = 8) -> list[PageSelectionResult]:
    """Used by Inc 2 retrieval. Postgres FTS over (title || aliases || summary).
    Returns top_k pages with relevance scores. NO LLM call here — LLM-based
    page-selection happens in Inc 2's retrieval router, layered on top of this."""
```

---

## 1.10 HTTP API

All under `/v1/projects/{project_id}/`. Auth + project_scope from Inc 0.

### Sources

- `POST /sources` — body `{title?, url?, metadata}`; creates Source row in `ingested` status
- `POST /sources/upload` — multipart; computes sha256, stores asset, creates SourceVersion, enqueues `normalize` worker job; returns Source with status `normalizing`
- `GET /sources` — list; filterable by status
- `GET /sources/{id}` — detail with current version + normalized doc summary
- `GET /sources/{id}/asset` — signed URL to MinIO/S3 (10min expiry)
- `POST /sources/{id}/reingest` — owner; triggers fresh normalization for current version (used when parser_version changes)
- `DELETE /sources/{id}` — owner; cascade: chunks, source_page, citations referencing it. Ledgered. UI surfaces a confirm dialog.

### Normalized + Chunks

- `GET /sources/{id}/normalized` — markdown + structure + quality flags
- `GET /sources/{id}/chunks` — paginated; for debugging + Inc 2 retrieval
- `POST /sources/{id}/chunks/search` — body `{query, top_k}`; intra-source search via cosine + FTS (used by Inc 2 descent)

### Wiki

- `GET /wiki/pages` — list; `kind`, `status`, `is_stub` filters
- `GET /wiki/pages/{id}?revision=...` — page (defaults to current revision)
- `GET /wiki/pages/by-slug/{slug}` — slug lookup
- `GET /wiki/pages/{id}/revisions` — revision list
- `GET /wiki/revisions/{id}` — specific revision detail
- `POST /wiki/pages/{id}/sections/{anchor}/handedit` — mark as hand-edited
- `DELETE /wiki/pages/{id}/sections/{anchor}/handedit` — clear
- `GET /wiki/aliases` — list; query `?surface_form=...`
- `POST /wiki/aliases` — manual add (e.g. analyst notices an alias)
- `POST /wiki/feedback/rejection` — body `{concept_name, reason, rejected_revision_id?}`
- `GET /wiki/feedback/rejection?concept_name=...` — list pending feedback for a concept
- `GET /wiki/source-pages` — list; one per Source

### Wiki index (used by Inc 2)

- `POST /wiki/search` — body `{query, top_k}`; FTS over WikiIndex; returns `[{page_id, title, summary, score, wikilinks_out}]`

### Connectors

- `GET /connectors` — global registry list
- `GET /connectors/bindings` — per-project allowlist
- `POST /connectors/bindings` — owner; enable/configure a connector for the project

### Wiki ingest control

- `POST /sources/{id}/wiki-ingest` — owner/editor; manually trigger the wiki ingest workflow for a source (normally auto-triggered after normalization). Returns `agent_run_id`.

---

## 1.11 Worker jobs

```python
# apps/workers/src/aleph_workers/jobs/normalize.py
async def normalize_job(ctx, source_id_str: str, agent_token: str) -> dict:
    """1. Fetch SourceVersion + asset bytes (verify sha256).
       2. Pick normalizer by mime_type.
       3. Produce markdown + structure + quality_flags.
       4. Store markdown to MinIO; insert NormalizedDocument row.
       5. Update Source.status="normalized", source_version.normalized_document_id.
       6. Enqueue chunk_embed_job + wiki_ingest_job.
       7. Ledger event normalization.completed.
    """

# jobs/chunk_embed.py
async def chunk_embed_job(ctx, normalized_doc_id_str: str, agent_token: str) -> dict:
    """1. Load markdown.
       2. Chunk per §1.7 algorithm.
       3. Batch-embed via LiteLLMClient.embed(capability="embedding").
       4. Insert DocumentChunk rows.
       5. Upsert RetrievalIndexRecord.
       6. Update Source.status="indexed".
       7. Ledger events: chunks.created, embeddings.completed.
    """

# jobs/wiki_ingest.py
async def wiki_ingest_job(ctx, normalized_doc_id_str: str, agent_token: str) -> dict:
    """Run the WikiIngestWorkflow (LangGraph). Returns the AgentRun summary."""
```

`agent_token` is minted by `aleph-api` when the job is enqueued (via the `/v1/agent-tokens` endpoint from Inc 0). Workers use the token to authenticate back into `aleph-api` for any service calls they make.

---

## 1.12 Frontend additions

The 3-panel shell stays. Two right-panel tabs get populated in Inc 1 (the other three remain placeholders):

- **Wiki tab** — `WikiIndexView` lists pages with kind/status filters. Click a page → `WikiPageView` (plain Markdown rendering with `[[wikilink]]` chips that navigate within the wiki, and `[c12]` markers that hover-preview the citation via tooltip). Sections marked hand-edited show a small `✎` badge with a clear button (owner/editor only).
- **Sources tab** — Sources list with status (ingested → normalized → indexed → wiki_done | failed). Click a source → detail view with normalized markdown preview + chunks debug pane + reingest button.

Center panel — chat remains empty/disabled with a note "Chat lands in Increment 2." Activity card shows running workers via SSE (`GET /v1/projects/{id}/agent-runs` polled or streamed).

A2UI is **not** wired in Inc 1. Pages render via plain React + `react-markdown` + custom wikilink renderer. A2UI integration in Inc 4 will replace this with `WikiSurface`.

---

## 1.13 Tests

### Unit

- `aleph-rks/tests/test_chunking.py` — chunking determinism, overlap correctness, heading preservation, code-block handling
- `aleph-rks/tests/test_normalization.py` — each format yields reasonable markdown; quality_flags fire on degraded sources
- `aleph-rks/tests/test_embedding.py` — batching, retry, error propagation; uses an httpx mock for LiteLLM
- `aleph-wiki/tests/test_wiki_service.py` — commit_revision idempotency, hand-edit splicing, claim/citation insertion, no-op on identical body
- `aleph-wiki/tests/test_alias_service.py` — surface form normalization, alias resolution, broken-link repair
- `aleph-wiki/tests/test_index_service.py` — FTS over title+aliases+summary returns expected results
- `aleph-wiki/tests/test_workflow.py` — wiki ingest workflow with mocked LLM calls; verify all 7 nodes execute in order and the right number of pages/claims/citations land

### Integration (`tests/e2e/`)

- `test_upload_to_wiki.py` — POST a small PDF → wait for status `wiki_done` → verify Source, SourceVersion, SourceAsset, NormalizedDocument, DocumentChunks, SourcePage, at least one stub topic page, WikiIndex rows, Aliases, ledger events for each phase, cost ledger entries for the LLM calls. End-to-end.
- `test_hand_edit_preservation.py` — POST a doc → wiki_done → mark a section hand-edited → re-trigger ingest (via reingest) → verify the marked section is preserved byte-for-byte while other sections may change.
- `test_rejection_feedback.py` — POST a doc → wait for wiki_done → write a RejectionFeedback for one concept → reingest → verify the new revision's prompt context included the rejection reason (via trace inspection) AND the rejection feedback row's `addressed_in_revision_id` is set.
- `test_immutable_revisions.py` — try to UPDATE wiki_revisions → expect trigger to raise.
- `test_chunk_search.py` — POST `/sources/{id}/chunks/search` returns relevant chunks for a known query in a fixture corpus.
- `test_wiki_search.py` — POST `/wiki/search` returns pages with title/alias/summary matches.
- `test_normalize_failure.py` — POST a known-broken PDF → status `failed` with `failure_reason`; no chunks created; UI surfaces the error.
- `test_embedder_change_reembed.py` — change ModelProfile.embedding for a project → re-embed job triggered → new chunks have new `embedder_model`; RetrievalIndexRecord updated.

### Fixture corpus

`tests/fixtures/inc1/`:

- `arxiv-2410.0xxxx-sample.pdf` (a few pages from a real arXiv paper, license-permitting)
- `wikipedia-region-x.html` (a snapshot from a public domain source)
- `notes.md` (handwritten markdown sample)
- `report.docx` (small generated DOCX)
- `broken.pdf` (deliberately corrupt; for failure-path test)

### Eval (introduced here, expanded in Inc 8)

`packages/aleph-evals/datasets/inc1_wiki_skeleton/`:

- `coverage_minimum.jsonl` — `{source_path, expected_concepts: [...]}`. Run: ingest the source, check WikiIndex contains a page per expected concept. Gate: 90% coverage.
- `alias_extraction.jsonl` — `{source_path, expected_aliases: [{surface, canonical}]}`. Gate: 80% extracted.

CI runs these against both `aleph-dev` and `aleph-production` profiles (Inc 0 set up profile-aware eval gates).

---

## 1.14 Documentation

Written/updated in this increment:

- `docs/domain/rks.md` — explains Source / Version / Asset / NormalizedDocument / DocumentChunk + storage layout + the chunking-is-for-intra-source-only rule
- `docs/domain/wiki.md` — explains WikiPage/Revision/Section/Link/Claim/SourcePage/Alias/HandEditMark/RejectionFeedback/WikiIndex + the immutable-revision rule + the SourcePage bridge concept
- `docs/domain/claims-and-provenance.md` — Claim → Citation → DocumentChunk[] | SourcePage edge; how confidence states evolve
- `docs/pipelines/normalization.md` — per-MIME pipeline, parser_version tracking, quality_flags
- `docs/pipelines/chunking-and-embedding.md` — algorithm, batch sizes, re-embed semantics
- `docs/agents/wiki-agent.md` — the LangGraph DAG, every node's contract, prompts overview, hand-edit and rejection-feedback wiring
- `docs/wiki/hand-edits.md` — analyst-facing how-to
- `docs/wiki/rejection-feedback.md` — analyst-facing how-to (note: full UI lands in Inc 5; Inc 1 supports via API)
- `docs/wiki/aliases.md` — alias extraction + repair semantics
- `docs/connectors/upload.md` — Upload connector behavior
- `docs/implementation-log.md` — Inc 1 entry

---

## 1.15 Acceptance criteria

Increment 1 is **done** when all hold:

1. **End-to-end upload → wiki.** Upload a PDF via the UI. Source goes `ingested → normalized → indexed → wiki_done`. The Wiki tab shows the new `SourcePage` and at least one stub topic page derived from the source. Ledger has events for every phase. Cost ledger has entries for every LLM call.
2. **Intra-source descent works.** `POST /v1/projects/{id}/sources/{source_id}/chunks/search` with a query that should match returns the right top-k chunks.
3. **Wiki page-selection works.** `POST /v1/projects/{id}/wiki/search` with a query returns matching pages from `WikiIndex` ordered by FTS relevance.
4. **Hand-edits preserved.** Mark a section hand-edited. Re-trigger wiki ingest for the source. The hand-edited section is unchanged byte-for-byte. Other sections may differ.
5. **Rejection feedback consumed.** Write a `RejectionFeedback` row. Re-trigger ingest. The new revision's prompts included the rejection reason (verifiable in the Langfuse span). The feedback row's `addressed_in_revision_id` is set.
6. **Revisions immutable.** UPDATE/DELETE on `wiki_revisions` raises.
7. **Re-embed on profile change.** Change `ModelProfile.embedding` for a project. A re-embed job runs against existing chunks. `DocumentChunk.embedder_model` is updated for all chunks of project sources. `RetrievalIndexRecord` rows updated.
8. **Aliases extracted and used.** Alias rows populated; `[[PC]]` in a body resolves to `[[Program Counter]]` when ingested via the alias.
9. **Eval gates pass.** Both `coverage_minimum.jsonl` and `alias_extraction.jsonl` eval datasets pass under both ModelProfiles.
10. **Permission leakage zero.** Member of Project X can not list/read sources or wiki pages of Project Y. 404 (not 403).
11. **Failure paths visible.** A broken PDF lands in `failed` status with a clear `failure_reason`. UI shows the error.
12. **Docs complete.** All files in §1.14 exist.
13. **No placeholders.** No `TODO/FIXME/NotImplementedError/pass` in production paths. Tests-only stubs OK.
14. **Implementation log written.** `docs/implementation-log.md` has the Inc 1 entry.

---

## 1.16 Handoff to Increment 2

Inc 2 will:

- Add `AssistantThread`, `AssistantMessage`, `AssistantSession`
- Implement the wiki retrieval router (LLM-routed page-selector on top of `IndexService.select_pages` + 1-hop `WikiLink` expansion + answer composer)
- Implement intra-source descent (consumes the chunk search endpoint from Inc 1)
- Wire the center-panel chat UI
- Surface cost banner + budget enforcement in chat

Inc 2 reuses:
- `WikiIndex` and `IndexService.select_pages` (page-selection FTS exists; Inc 2 layers an LLM call on top)
- `WikiPage` + `WikiLink` (graph traversal)
- `DocumentChunk` + the `chunks/search` endpoint (intra-source descent)
- `Citation` (preserve `[c12]` markers in chat output)
- `SourcePage` (`[[Source:X]]` becomes a hover-previewable wikilink in chat)

No schema changes to Inc 1 entities are anticipated for Inc 2. If any are needed, they go through a new Alembic migration, never by editing Inc 1's migration.

See `docs/superpowers/specs/2026-05-27-inc-2-wiki-first-chat-design.md`.
