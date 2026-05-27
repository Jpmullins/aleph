# Increment 7 — Builder Agent + RenderedAssets + Artifacts + Export

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md`
**Depends on:** Inc 0–6
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 7.1 Scope

Increment 7 ships outputs. The Builder agent composes approved wiki content, claims, citations, datasets, and pinned cards into exportable artifacts (PDF / DOCX / markdown report bundles / source packs / cited slide decks). The `RenderedAsset` table records every PNG/SVG/PDF rendered from cards. The `ArtifactsSurface` (catalog-defined in Inc 4) gets real backing data.

After Inc 7, an analyst can ask "build me a report on Region X" and get a cited PDF with embedded charts, a source appendix, a bibliography, and a verifiable lineage chain back to immutable wiki revisions and dataset versions.

### In scope

- `RenderedAsset` model + Playwright-based render service (sandboxed)
- PNG / SVG / PDF rendering from A2UI Cards (Chart/Table/Map/Graph)
- `Artifact` + `ArtifactVersion` models
- Builder agent (LangGraph) — composes from approved wiki + datasets + cards
- Export formats: PDF, DOCX, markdown-bundle (ZIP with assets), source-pack (ZIP of original SourceAssets + normalized + license info)
- CSL-aware bibliography output (CSL-JSON metadata in `Source.source_metadata_jsonb`; Builder formats per chosen CSL style)
- Lineage view: every Artifact carries an explicit ref to the wiki revisions, dataset versions, source versions, and rendered asset IDs that composed it
- `ArtifactsSurface` fully functional with browser + viewer + export controls
- Tests + docs + eval

### Out of scope

- Eval suite expansion / regression gates → Inc 8

### Dependencies

- Inc 0–6 fully
- A2UI catalog (Inc 4) — Builder uses card schemas to compose embedded renders
- DatasetVersion (Inc 6) — chart renders snapshot the same immutable version
- ApprovalRequest (Inc 5) — Builder can require export approval for sensitive artifacts (default off; per-project policy)

### Downstream

- Inc 8 ships regression evals against artifact lineage correctness + cost-drift gates

---

## 7.2 Repository changes

```
packages/
└── aleph-artifacts/                    # new package
    └── src/aleph_artifacts/
        ├── __init__.py
        ├── models.py                   # RenderedAsset, Artifact, ArtifactVersion
        ├── artifact_service.py
        ├── render_service.py           # invokes Playwright workers
        ├── builder/
        │   ├── __init__.py
        │   ├── workflow.py             # LangGraph
        │   ├── templates/
        │   │   ├── report_pdf.md.j2    # jinja2 template
        │   │   ├── report_docx.md.j2
        │   │   ├── deck.md.j2
        │   │   └── source_pack.j2      # manifest
        │   ├── nodes/
        │   │   ├── outline.py
        │   │   ├── section_compose.py
        │   │   ├── citation_resolve.py
        │   │   ├── chart_freeze.py
        │   │   ├── bibliography.py
        │   │   └── package.py
        │   └── prompts/
        ├── csl/
        │   ├── __init__.py
        │   ├── styles/                 # bundled CSL styles (apa-7, chicago-author-date, ieee, vancouver, custom)
        │   └── formatter.py            # CSL-JSON → formatted bibliography
        └── exporters/
            ├── pdf.py                  # md → PDF via WeasyPrint or Prince (configurable)
            ├── docx.py                 # md → DOCX via pandoc + python-docx
            ├── markdown_bundle.py      # ZIP of markdown + assets
            └── source_pack.py          # ZIP of raw + normalized

apps/workers/src/aleph_workers/jobs/
├── builder.py                          # run Builder LangGraph workflow
└── render_card.py                      # Playwright worker job

apps/api/src/aleph_api/routes/
├── artifacts.py                        # CRUD + export
└── renders.py                          # RenderedAsset GET, signed URL

apps/web/src/
└── a2ui/components/
    └── ArtifactsSurface.tsx            # full impl (was shell in Inc 4)
```

Dependencies:

- Python: `playwright`, `weasyprint`, `pypandoc` (already from Inc 1), `python-docx` (already), `jinja2`, `python-jose` (already), CSL libs (`citeproc-py` or call out to Citation.js via node)
- System: `playwright install chromium`, `pandoc`, optional `prince` for higher-fidelity PDFs (configurable per project)

---

## 7.3 Domain model

```python
# packages/aleph-artifacts/src/aleph_artifacts/models.py

class RenderedAsset(CommonColumns, Base):
    """Frozen PNG/SVG/PDF render of a card or composite. Records what was rendered
    so the export is reproducible from the spec + data snapshot."""
    __tablename__ = "rendered_assets"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # card | wiki_page | composite
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    # InteractiveCard.id | WikiPage.id | Artifact.id
    source_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # the specific version frozen (e.g. WikiRevision.id, InteractiveCardVersion.id)
    dataset_version_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # all DatasetVersions referenced by this render
    output_format: Mapped[str] = mapped_column(String(16), nullable=False)
    # png | svg | pdf
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    width_px: Mapped[int | None] = mapped_column(nullable=True)
    height_px: Mapped[int | None] = mapped_column(nullable=True)
    bytes: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    render_spec_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # the exact A2UI payload + viewport that was rendered; reproducible

class Artifact(CommonColumns, Base):
    __tablename__ = "artifacts"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    # A0001 — referenced as [[Artifact:A0001]] in wiki/notes
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # report_pdf | report_docx | report_markdown_bundle | source_pack | deck_pdf
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)

class ArtifactVersion(Base):
    """Immutable. Each build creates a new version."""
    __tablename__ = "artifact_versions"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    artifact_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    bytes: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    lineage_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # complete lineage record: wiki_revision_ids, dataset_version_ids,
    # source_version_ids, rendered_asset_ids, agent_run_id, csl_style,
    # template_name, commit_message
    template_name: Mapped[str] = mapped_column(String(64), nullable=False)
    csl_style: Mapped[str] = mapped_column(String(64), nullable=False)
    builder_agent_run_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("artifact_id", "version_no"),)
# Immutable triggers, same as wiki_revisions
```

Migration `<timestamp>_inc7_artifacts.py` creates the three tables with immutability triggers on `rendered_assets` (no update; delete only via project cascade) and `artifact_versions`.

---

## 7.4 Playwright render service

A new worker job runs Playwright in a sandboxed container to produce PNG/SVG/PDF from A2UI Card specs.

### Sandbox

- Separate Docker image (`aleph-render`) with Playwright + chromium pre-installed
- Container runs with `--network none` except for an explicit allowlist of internal services
- **No Postgres or S3 credentials** in this container — receives a signed spec + signed data snapshot URL and returns bytes
- Read-only root filesystem; writable `/tmp`

### Render protocol

1. `aleph-api` calls `render_service.render(card_id, format, options)` → enqueues `render_card_job`
2. The Playwright worker fetches the card payload and data snapshot via signed URLs (5-min expiry)
3. Renders to PNG (chrome screenshot), SVG (Chrome `print` with `format=svg`-like path or via Vega's native SVG renderer for chart cards), or PDF (Chrome `printToPDF`)
4. Computes SHA-256
5. Uploads to MinIO/S3 at `s3://bucket/projects/{project_id}/renders/{card_id}/{format}/{sha256}.{ext}`
6. Writes `RenderedAsset` row via the callback (the worker has a short-lived agent token like AIQ tools do)
7. Returns the asset_id

### Reproducibility

`RenderedAsset.render_spec_jsonb` carries the **exact A2UI payload + data snapshot** used. Re-rendering from the same spec produces a byte-identical output (chromium version pinned in the `aleph-render` image; tested in CI).

---

## 7.5 Builder agent

`packages/aleph-artifacts/src/aleph_artifacts/builder/workflow.py` is a LangGraph workflow.

### State

```python
class BuilderState(TypedDict):
    agent_run_id: UUID
    project_id: UUID
    artifact_id: UUID
    artifact_kind: str
    template_name: str
    csl_style: str
    inputs: BuilderInputs  # what to include
    profile: ModelProfile

    outline: list[OutlineNode] | None
    sections: list[ComposedSection] | None
    citation_keys: list[CitationKey] | None
    chart_renders: list[RenderedAsset] | None
    bibliography: str | None
    packaged_bytes: bytes | None
    packaged_sha256: str | None
```

### Inputs

```python
class BuilderInputs(BaseModel):
    title: str
    abstract: str | None
    wiki_page_ids: list[UUID]                # included sections (in order)
    extra_topics: list[str]                  # topics to draft from scratch (via wiki agent)
    embed_chart_card_ids: list[UUID]
    embed_table_card_ids: list[UUID]
    embed_map_card_ids: list[UUID]
    embed_graph_card_ids: list[UUID]
    include_source_pack: bool = False
    include_appendix: bool = True
    csl_style: str = "apa-7"
    template_name: str = "report_pdf"
```

### Nodes

1. **`outline`** — LLM call (`synthesis`) to produce a coherent section order from the inputs; respects wiki page order if specified; returns `OutlineNode[]`
2. **`section_compose`** — for each section (sequential to maintain narrative): compose section markdown from approved wiki revisions; never invents content; preserves `[c…]` markers and resolves them against the lineage's source set
3. **`citation_resolve`** — collect every cited claim → source mapping; build CSL-JSON bibliography source list from `Source.source_metadata_jsonb.csl_json` fields; flag missing CSL metadata (deterministic, doesn't fail; just warns)
4. **`chart_freeze`** — for every embedded chart/table/map/graph card, call `render_service.render` to produce PNG (default; PDF if vector preferred); collect `RenderedAsset.id`s
5. **`bibliography`** — call `csl.formatter.format(sources, style=csl_style)` to produce formatted bibliography section
6. **`package`** — assemble final document:
   - markdown template (jinja2) → filled with composed sections + embedded asset references
   - exporter for the target format (`pdf` → WeasyPrint or Prince; `docx` → pypandoc; `markdown_bundle` → ZIP with assets; `source_pack` → ZIP of raw assets)
   - Upload to S3, compute sha256, write `ArtifactVersion` row with full `lineage_jsonb`
7. **`commit`** — set `Artifact.current_version_id`; ledger event `artifact.version.commit`

### Approval gate

`Project.builder_approval_required` (new field on Project) — when true, the Builder workflow pauses before `package` and creates an `ApprovalRequest` with target_kind=`artifact_version` carrying the outline + sections + bibliography preview. Owner approves → workflow resumes; rejects → ledger event + halt.

Default: false. Sensitive projects (e.g. OSINT export to external stakeholders) flip it on.

### Costs

Builder is expensive (per-section synthesis calls + chart renders). Pre-flight cost estimation per Inc 0 §9.2 applies; user confirms before workflow starts if estimated cost would exceed remaining budget headroom.

---

## 7.6 CSL bibliography

Sources carry CSL-JSON metadata in `Source.source_metadata_jsonb.csl_json`:

```json
{
  "type": "article-journal",
  "title": "...",
  "author": [{"family": "Smith", "given": "Jane"}, ...],
  "container-title": "Journal of X",
  "issued": {"date-parts": [[2024, 5]]},
  "DOI": "10.xxxx/yyyy",
  "URL": "https://..."
}
```

Connectors populate `csl_json` at ingest:

- **arXiv**: full CSL from arXiv metadata API
- **OpenAlex / Semantic Scholar**: native CSL output
- **Tavily / Exa / Serper / RSS**: best-effort (title + URL + date); flag as `csl_partial=true`
- **Upload**: optional analyst entry; if missing, treats as `partial`

CSL styles bundled at `packages/aleph-artifacts/src/aleph_artifacts/csl/styles/`:

- apa-7 (default)
- chicago-author-date
- ieee
- vancouver
- a "project-custom" slot that loads a CSL file from project settings

Citeproc engine: `citeproc-py` (Python) for the default formatter. CI-tested against a fixture corpus of all 4 styles.

---

## 7.7 ArtifactsSurface (full implementation)

The `ArtifactsSurface` (Inc 4 shell) gains:

- A list of project artifacts grouped by `artifact_kind`
- A detail pane showing:
  - Title, description, current version, lineage chip ("3 wiki revisions, 2 datasets, 14 sources")
  - Preview (PDF inline via PDF.js; DOCX via download-only; markdown_bundle list with download)
  - Export controls (download current, list version history, build new version)
  - Lineage view (clickable; click a wiki revision → opens WikiSurface at that revision)
- A "New artifact" affordance:
  - Owner/editor only
  - FormCard wizard: title, kind, template, csl_style, include wiki pages (multi-select), include cards (multi-select), include source pack toggle, approval-required toggle
  - Submit → invokes Builder agent (returns agent_run_id; SSE progress shown)

---

## 7.8 HTTP API

All under `/v1/projects/{project_id}/`.

### Artifacts

- `POST /artifacts` — body `{title, artifact_kind, description?}`; creates empty Artifact (no version yet)
- `GET /artifacts` — list; filters
- `GET /artifacts/{id}` — detail with version list
- `DELETE /artifacts/{id}` — soft delete; cascades versions; ledgered

### Artifact build

- `POST /artifacts/{id}/build` — body = `BuilderInputs`; dispatches Builder workflow; returns `{agent_run_id}`
- `GET /artifacts/{id}/builds/{agent_run_id}` — status + events
- `GET /artifacts/{id}/builds/{agent_run_id}/stream` — SSE
- `POST /artifacts/{id}/builds/{agent_run_id}/approve` — owner; only relevant if approval_required=true; resumes
- `POST /artifacts/{id}/builds/{agent_run_id}/reject` — owner; halts

### Artifact versions

- `GET /artifacts/{id}/versions` — list
- `GET /artifacts/{id}/versions/{vid}` — detail with full lineage_jsonb
- `GET /artifacts/{id}/versions/{vid}/download` — signed URL (10min)
- `GET /artifacts/{id}/versions/{vid}/lineage` — structured lineage with hyperlink IDs

### Renders

- `GET /renders/{id}` — RenderedAsset detail
- `GET /renders/{id}/file` — signed URL to bytes
- `POST /renders/preview` — preview a card render without persisting (returns bytes inline); used by ArtifactsSurface preview

---

## 7.9 Lineage as a first-class view

Every `ArtifactVersion.lineage_jsonb` carries:

```json
{
  "wiki_revisions": [{"page_id": "...", "revision_id": "...", "title": "..."}],
  "dataset_versions": [{"dataset_id": "...", "version_id": "...", "name": "..."}],
  "source_versions": [{"source_id": "...", "version_id": "...", "title": "...", "csl_json": {...}}],
  "rendered_asset_ids": ["..."],
  "agent_run_id": "...",
  "csl_style": "apa-7",
  "template_name": "report_pdf",
  "commit_message": "Built v1 from approved Region X wiki + AA chart",
  "approvals": [{"approval_request_id": "...", "decided_by": "...", "decided_at": "..."}]
}
```

This makes "what produced this artifact" auditable and reproducible. Re-running the builder against the **same lineage** must produce a byte-identical document (modulo random IDs in PDF metadata, which the exporter normalizes by passing a fixed `pdf:CreationDate`).

---

## 7.10 Tests

### Unit

- `aleph-artifacts/tests/test_builder_workflow.py` — every node with mocked LLM/render produces expected output
- `aleph-artifacts/tests/test_outline_composition.py` — outline LLM produces sensible section order for a fixture input
- `aleph-artifacts/tests/test_csl_formatter.py` — each bundled style formats fixture sources correctly
- `aleph-artifacts/tests/test_chart_freeze.py` — card → render_service mock invoked correctly; RenderedAsset row written
- `aleph-artifacts/tests/test_exporters.py` — PDF, DOCX, markdown_bundle, source_pack each produce valid bytes
- `aleph-artifacts/tests/test_lineage_reproducibility.py` — given same lineage, exporter produces same sha256 (with fixed PDF metadata)

### Integration (`tests/e2e/`)

- `test_build_pdf_report.py` — Approve a wiki page → POST /artifacts/{id}/build with wiki_page_ids → wait → version 1 exists → PDF download → opens → contains expected sections + bibliography in expected style
- `test_chart_in_report.py` — Include a ChartCard pinned to a wiki page → built PDF embeds the rendered PNG → RenderedAsset row exists with correct dataset_version_id snapshot
- `test_build_with_approval.py` — Toggle approval_required → build workflow pauses at `package` → ApprovalCard appears in Briefs → approve → resumes; reject → halts
- `test_lineage_view.py` — Fetch lineage → click through to wiki revision → opens at that revision
- `test_reproducible_build.py` — Build artifact v1 → record lineage + sha256 → build a new version with the EXACT same lineage → sha256 matches
- `test_source_pack.py` — Build source_pack artifact → ZIP contains raw assets + normalized markdown + license manifest
- `test_render_sandbox_no_creds.py` — Verify aleph-render container has no DB/S3 credentials in env; render calls only succeed via signed URL + callback
- `test_permission_leakage_artifacts.py` — Project isolation

### Eval (`packages/aleph-evals/datasets/inc7_artifacts/`)

- `bibliography_correctness.jsonl` — `{sources, csl_style, expected_first_entries}`. Gate: 100% match on first line of each entry across all 4 default styles.
- `lineage_completeness.jsonl` — build artifact, assert lineage_jsonb references every contributing wiki_revision/dataset_version/source.
- `render_pixel_diff.jsonl` — render same card spec twice; gate: pixel-diff ≤ 0.1% (allows minor antialiasing).

---

## 7.11 Documentation

- `docs/agents/builder-agent.md` — workflow, prompts, inputs
- `docs/artifacts/rendered-assets.md` — render sandbox, signed-URL flow, reproducibility
- `docs/artifacts/templates.md` — jinja2 template structure, how to add a template
- `docs/artifacts/csl-styles.md` — bundled styles, custom CSL upload
- `docs/artifacts/source-pack.md` — what's in it + license manifest format
- `docs/security/render-sandbox.md` — egress restrictions, signed URLs, no-creds policy
- `docs/ui/artifacts-surface.md`
- `docs/implementation-log.md` — Inc 7 entry

---

## 7.12 Acceptance criteria

1. **Build PDF report.** Approve a wiki page → build → download PDF → opens with the page content + a bibliography in the chosen CSL style.
2. **Chart embed.** Include a ChartCard → PDF contains the rendered PNG at the right position; `RenderedAsset.dataset_version_ids` snapshots the exact DatasetVersion.
3. **Build DOCX.** Same flow for DOCX → opens cleanly in Word/Libre Office; bibliography formatted.
4. **Build markdown bundle.** ZIP contains markdown + asset folder + lineage.json + license manifest.
5. **Source pack.** ZIP of raw SourceAssets + normalized markdown + license info.
6. **Reproducible.** Build twice from identical lineage → byte-identical artifact (modulo normalized PDF metadata).
7. **Render sandbox.** aleph-render container has no DB/S3 creds; all reads via signed URL; writes via callback.
8. **Approval gate.** Toggling approval_required pauses + creates ApprovalRequest; approval resumes; rejection halts.
9. **Lineage complete.** lineage_jsonb references every contributing wiki_revision, dataset_version, source_version, rendered_asset.
10. **ArtifactsSurface live.** Browse, view, build, download.
11. **CSL styles.** All 4 default styles format correctly.
12. **Permission leakage zero.**
13. **Eval gates pass.**
14. **Docs complete.**
15. **No placeholders.**
16. **Implementation log written.**

---

## 7.13 Handoff to Increment 8

Inc 8 ships the full eval suite, UserFeedback inline affordances, and regression gates in CI. Inc 7 already laid `lineage_completeness` and `render_pixel_diff` evals; Inc 8 expands the suite to cover the whole product end-to-end.

No schema changes to Inc 7 entities anticipated.

See `docs/superpowers/specs/2026-05-27-inc-8-eval-feedback-gates-design.md`.
