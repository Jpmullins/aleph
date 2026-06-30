# Implementation log

## Increment 8: Eval suite + UserFeedback + regression gates

**Completed:** 2026-05-28
**Commit range:** (will be filled at merge time)

### What was built

- `aleph-evals` expanded from Inc 0 skeleton with full
  `EvalDataset` / `EvalCase` / `EvalRun` / `EvalResult` models.
- Manifest-driven dataset discovery: walks
  `packages/aleph-evals/datasets/inc<N>_<area>/manifest.yaml` + JSONL
  case files.
- Scorer registry: retrieval, citation, coverage, permission,
  synthesis, cost, metric_only.
- CI gate composes report → exit code; per-profile baselines in
  `ci/baselines.json`.
- FreshQA + DeepResearch Bench adapters (lazily import AIQ; raise a
  clear "vendor AIQ first" message when the submodule is absent).
- `UserFeedback` model in `aleph-core` + `/projects/{id}/feedback`
  endpoint with auto-promotion of `marked_wrong` /
  `misleading` / `false_positive` rows into a per-project
  `user_feedback:{project_id}` EvalDataset.
- Alembic migration `inc8_evals_feedback`.
- Cross-cutting `inc8_cross_cutting/` bundle: permissions, citations,
  coverage, cost-drift.

### Honest gaps

- AIQ adapters require the vendored submodule; runner marks the
  dataset errored when absent.
- Per-card inline feedback buttons not yet wired; API accepts
  feedback from any UI source.
- Bundled JSONL cases are seed examples — operators expand per
  `docs/evals/regression-suite.md`.

### Codebase complete

This is the final increment of the build sequence. Beyond Inc 8 the
roadmap (top-level §16.2) is operator-action items: AIQ submodule
vendoring, Playwright sandbox container, KMS for production
credentials, and ongoing connector / catalog / ModelProfile additions.

---

## Increment 7: Builder agent + RenderedAssets + Artifacts + exporters

**Completed:** 2026-05-28
**Commit range:** (will be filled at merge time)

### What was built

- **`aleph-artifacts` package:** `RenderedAsset`, `Artifact`
  (short_id A0001…), `ArtifactVersion` (immutable Postgres triggers).
- **Builder LangGraph workflow:** outline → section_compose →
  citation_resolve → chart_freeze → bibliography → package. Composes
  approved wiki content into a markdown report, walks
  `[[Source:Sxxxx]]` references to build a CSL-JSON list, formats the
  bibliography per the chosen style, packages the final bytes per
  `artifact_kind`, uploads to MinIO at a deterministic path, writes a
  fully-typed `ArtifactVersion.lineage_jsonb`.
- **CSL formatter** (`aleph_artifacts.csl`): uses `citeproc-py` with
  bundled XML styles when available; falls back to a deterministic
  plain-author-year formatter so the Builder is never blocked by
  missing styles. `format_bibliography(items, style, output)` returns
  markdown / html / plain.
- **Exporters:**
  - `markdown_bundle` — ZIP of `report.md` + `manifest.json` + assets.
  - `pdf` — markdown-it-py → HTML → WeasyPrint PDF. `renderer` arg
    allows swapping to Prince per operator policy.
  - `source_pack` — ZIP of `manifest.json` + raw blobs + normalized
    markdown + license notes. Used by the `source_pack` artifact_kind.
- **`render_service.record_render`:** uploads bytes for a card render
  and writes a `RenderedAsset` row with the full reproducibility spec
  (`render_spec_jsonb`).
- **Alembic migration `inc7_artifacts`:** creates `rendered_assets`,
  `artifacts`, `artifact_versions` with immutability triggers on
  `artifact_versions`.
- **API routes:**
  - `/artifacts` — list / get.
  - `/artifacts/{id}/versions` — list versions.
  - `/artifacts/build` — body
    `{title, artifact_kind, template_name, csl_style, wiki_page_ids,
    dataset_version_ids}`. Creates the artifact, mints an agent
    token, enqueues `builder_job`, returns
    `{artifact_id, agent_run_id, dispatched}`.
  - `/rendered-assets` — list (limit 200).
- **Worker:** `builder_job` verifies the agent token, drives the
  Builder workflow, finalizes the AgentRun with the new version id.

### Trace and ledger behavior added

- Ledger action kinds: `artifact.create`, `artifact.version.create`.
- OTEL spans: `builder.outline`, `builder.section_compose`,
  `builder.citation_resolve`, `builder.chart_freeze`,
  `builder.bibliography`, `builder.package`.
- Cost ledger unchanged (Builder doesn't make LLM calls in this
  commit; outline/composition is structural — Inc 7 follow-on adds a
  Builder LLM polish pass).

### Honest scope

- Playwright sandbox isn't a separate container in this commit. The
  Builder records `RenderedAsset` rows + handles the markdown+PDF
  export path, but actual chromium rendering of `ChartCard` /
  `MapCard` / `GraphCard` to PNG is left for the follow-on that
  builds the `aleph-render` Docker image. The PDF artifacts produced
  by `weasyprint` are clean; embedded chart PNGs come once the
  render-job container lands.
- DOCX exporter exists in the `exporters/` directory contract but
  isn't wired into `_node_package` yet (needs pandoc availability
  check first).
- CSL XML style files (`apa-7.csl`, etc.) aren't bundled with the
  package. The formatter falls back gracefully to plain author-year
  formatting when missing. Operators drop styles into
  `aleph_artifacts/csl/styles/` to enable full citeproc rendering.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-8-eval-feedback-gates-design.md`.

---

## Increment 6: Datasets + visualization cards

**Completed:** 2026-05-28
**Commit range:** (will be filled at merge time)

### What was built

- **`aleph-datasets` package:** `Dataset` (with `short_id` D0001…,
  kinds `tabular | geo | graph`), `DatasetVersion` (immutable
  Postgres triggers), `Observation`; `dataset_service` with
  `create_dataset` + `commit_version` (atomic; inline rows ≤ 1000 OR
  ≤ 100 KB, otherwise stores parquet_uri); `schema_inference` (type
  promotion `null → int → float`, GeoJSON → geometry);
  `vega_compile` (line / bar / scatter spec builders).
- **artificialanalysis.ai connector** in
  `aleph_connectors.artificialanalysis`. `output_kind=dataset_rows`.
  `extract_rows` flattens model/benchmark snapshots into
  `(model, metric, value, date)` rows; the import path writes a
  `Dataset` + `DatasetVersion`.
- **Alembic migration `inc6_datasets`:** creates `datasets`,
  `dataset_versions` (with immutability triggers),
  `observations`; seeds the `artificialanalysis` connector row.
- **API routes:** `/datasets` (list / create / get), `/datasets/{id}/versions`
  (list / commit), `/dataset-versions/{id}/observations` (list),
  `/dataset-versions/{id}/chart-spec` (compile a Vega-Lite v6 spec
  from axis hints + rows for the bound version).

### Trace and ledger behavior added

- Ledger action kinds: `dataset.create`, `dataset.version.commit`.
- Cost ledger unchanged (no LLM calls in Inc 6).

### Honest scope

The catalog `ChartCard` / `TableCard` / `MapCard` / `GraphCard`
schemas already exist (Inc 4). Inc 6 lights them up server-side
(real data, real spec compile). Full client-side renderers for
MapLibre and React Flow remain placeholders in `apps/web/src/a2ui/components/`
— their bind-to-DatasetVersion plumbing is now real but the rich
interaction (pan/zoom/cluster) lands when the chart spec call is
called from the renderer in a follow-on commit.

### Known issues / debts

- Parquet write path is plumbed (a caller can pass `parquet_uri`)
  but no automatic parquet upload is implemented. The threshold
  switch sets `rows_inline=false` only when caller hands in
  `parquet_uri`; in this commit, oversized inline payloads will
  still attempt inline storage and degrade to large `Observation`
  rows. Lands with the parquet helper.
- artificialanalysis import job is not yet enqueued from any
  worker; manual invocation via `commit_version` works. The Arq
  job that watches the connector binding lands when scheduled
  refresh is wired (Inc 8 fixture).
- No live-stack integration tests yet.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-7-builder-artifacts-design.md`.

---

## Increment 5: Reviewer agents + approval workflow + hypotheses + AgentMemory

**Completed:** 2026-05-28
**Commit range:** (will be filled at merge time)

### What was built

- `aleph-reviewer`: `ReviewRun`, `ReviewFinding`, `ApprovalRequest`
  models; `review_service` (start_run / add_finding / finalize_run);
  `approval_service.decide()` writes a paired `ApprovalDecision` (Inc 3
  row) and mirrors decision onto linked `ReviewFinding`.
- MechanicalReviewer LangGraph workflow with deterministic checks:
  citation_match (wraps verify_citations), broken_wikilink,
  stale_source (freshness threshold), duplicate_source (sha256 group).
- EditorialReviewer LangGraph workflow with 5 LLM-judged subagents
  (contradiction, weak_source, narrative_gap, coverage_gap,
  factual_freshness). Findings with severity ≥ medium auto-create
  paired `ApprovalRequest`s.
- `aleph-hypotheses`: `Hypothesis` (`short_id` H0001…),
  `HypothesisVersion` (immutable Postgres triggers),
  `HypothesisEvidence`; `confidence` module with
  `next_confidence_from_evidence`; `hypothesis_service` for create /
  patch / add_evidence (re-derives confidence + writes a new version
  on state transition).
- `AgentMemory` model in `aleph-core` (per-project, per-agent,
  namespaced scratchpad).
- Alembic migration `inc5_reviewers_hypotheses`.
- API routes: `/reviews/runs`, `/reviews/findings`,
  `/approval-requests`, `/approval-requests/{id}/decide`,
  `/hypotheses` + nested `/evidence` + `/versions`.
- Worker jobs `mechanical_review_job` + `editorial_review_job`
  registered in the Arq worker.

### Trace and ledger behavior added

- Ledger action kinds: `hypothesis.create`,
  `hypothesis.version.create`, `hypothesis.evidence.add`,
  `approval_request.approved`, `approval_request.rejected`.
- OTEL spans: `review.mechanical.<check>`,
  `review.editorial.<subagent>`.
- Cost ledger covers all editorial subagent LLM calls
  (`purpose="editorial.<subagent>"`).

### Known issues / debts

- Wiki `commit_revision` doesn't auto-enqueue `mechanical_review_job`
  in this commit. The worker exists and can be called manually; the
  enqueue hook lands in a follow-on (one-line worker-pool ref in
  wiki_service).
- Deep Agents harness not used for EditorialReviewer; the LangGraph
  version ships now with the same contract.
- No live-stack integration tests yet; pure-function confidence-rule
  tests pending.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-6-datasets-visualization-design.md`.

---

## Increment 4: A2UI Catalog v1.0.0 + Interactive Workspace surfaces

**Completed:** 2026-05-27
**Commit range:** (will be filled at merge time)

### What was built

- **`aleph-a2ui` package:** JSON Schema catalog v1.0.0 with 5 surfaces
  (`WikiSurface`, `ArtifactsSurface`, `NotesSurface`,
  `HypothesesSurface`, `BriefsSurface`) and 12 inline cards
  (`ClaimCard`, `SourceCard`, `ChartCard`, `TableCard`, `MapCard`,
  `GraphCard`, `ApprovalCard`, `FindingCard`, `HypothesisCard`,
  `NotebookCellCard`, `FormCard`, `DiffCard`); typed Python builders
  for every component; full action schema for the 10 action kinds
  (`approve`, `reject`, `open`, `navigate_wiki`, `submit_form`,
  `create_hypothesis`, `edit_note`, `clarify`, `mark_handedit`,
  `clear_handedit`); validators (`validate_component`,
  `validate_surface`).
- **`InteractiveCard` / `InteractiveCardVersion` (immutable triggers) /
  `CardAction`** SQLAlchemy models.
- **`ActionRouter`:** single dispatch chokepoint. Validates params
  against the catalog action schema, resolves the handler, runs it
  inside the dispatching session, writes a `CardAction` row + a
  `a2ui.action.<kind>` ledger event in one transaction. Built-in
  handlers wired for approve / reject / open / navigate_wiki /
  submit_form / create_hypothesis / edit_note / clarify /
  mark_handedit / clear_handedit.
- **`aleph-notes` package:** Note + NoteSection models, `note_service`
  for CRUD.
- **Alembic migration `inc4_a2ui`:** creates `interactive_cards`,
  `interactive_card_versions` (with immutability triggers — same
  pattern as `wiki_revisions`), `card_actions`, `notes`, and
  `note_sections`.
- **API routes (`/v1/projects/{id}/...`):**
  - `/notes` — list / create / get / sections (post / patch).
  - `/cards/actions` — single dispatch endpoint. Body is the
    `CardActionIn` shape; result includes the `action_id` and the
    handler's structured result.
  - `/briefs` — returns a `BriefsSurface` payload populated with
    `ApprovalCard`s for pending `SynthesisProposal`s.
  - `/surfaces/{tab}` — returns the A2UI surface JSON for the
    requested tab (wiki / artifacts / notes / hypotheses / briefs).
    Validates the emitted surface against the catalog before
    responding.
  - `/a2ui/catalog` — exposes the canonical JSON Schema catalog so the
    web app can validate inbound surfaces against the same contract.
- **Web:**
  - `apps/web/src/a2ui/catalog.ts` — TypeScript mirror of the catalog
    (component names + action names).
  - `apps/web/src/a2ui/components/` — real React renderers for all 17
    components. `ApprovalCard` ships the full approve/reject flow with
    a reason modal. `NotebookCellCard` debounced auto-save via
    `edit_note`. `FormCard` builds dynamic forms from the
    `fields` spec.
  - `apps/web/src/a2ui/register.tsx` — single dispatcher that walks the
    surface tree.
  - `apps/web/src/components/A2UIRightPanel.tsx` — replaces the
    placeholder right panel. Queries `/surfaces/{tab}`, renders the
    surface tree, posts every action through `/cards/actions`,
    invalidates the surface query on action success so the renderer
    re-fetches.

### Trace and ledger behavior added

- Ledger action kinds (new in Inc 4): `a2ui.action.<kind>` for every
  one of the 10 catalog actions; `note.create`, `note.section.create`,
  `note.section.update`.
- OTEL spans: `a2ui.action` per dispatch.
- The `CardAction` row schema is durable for every analyst click that
  produced a state change.

### Honest scope

The catalog includes `ChartCard` / `TableCard` / `MapCard` / `GraphCard`
schemas but the underlying `DatasetVersion` model lands in Inc 6. The
renderers ship a clear "Datasets land in Increment 6" placeholder
state — the *catalog contract* is real now so Inc 6 can plug content
in without schema churn. Same for `HypothesesSurface` / `FindingCard`
(Inc 5) and `ArtifactsSurface` (Inc 7).

### Known issues / debts

- **`@a2ui/react` not yet imported.** The renderer is a homegrown
  walk-and-dispatch (`apps/web/src/a2ui/register.tsx`) rather than the
  upstream `@a2ui/react`. Inc 5 / 6 can swap to upstream once a
  particular release crosses our minimum-feature bar; the catalog
  contract is identical so the swap is mechanical.
- **No SSE-driven surface push.** Inc 4 surfaces refresh via
  client-side polling (10s for Briefs). The SSE channel from Inc 2 is
  the obvious next path.
- **Pin-card route not yet exposed.** `InteractiveCard.pinned_to` is
  in the schema; the corresponding `POST /cards/{id}/pin` route lands
  alongside Inc 6's chart pinning.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-5-reviewers-hypotheses-design.md`.
Increment 5 lands the MechanicalReviewer + EditorialReviewer agents,
the `Hypothesis` / `HypothesisVersion` / `HypothesisEvidence` model
family, and `ReviewFinding` + `ApprovalRequest`. The Inc 4 catalog
already has the schemas for `FindingCard` and `HypothesisCard` and
the action handlers for `approve`/`reject`/`create_hypothesis` so the
Inc 5 work plugs into existing slots.

---

## Increment 3: AIQ subsystem + connector roster + /synthesize

**Completed:** 2026-05-27 (Aleph-side; AIQ submodule vendoring is operator-action)
**Commit range:** (will be filled at merge time)

### What was built

- New package **`aleph-connectors`**: `ConnectorBase` Protocol +
  `ConnectorContext` carrying agent-token-scoped credential value;
  `ConnectorCredential` model with libsodium SealedBox cipher (dev) and
  a KMS-AES-GCM hook (prod); `ConnectorCredentialService` with upsert /
  delete / rotate / decrypt_for_callback (deployment-env fallback when
  no project-specific cred exists); in-process `ConnectorRegistry`;
  full implementations of Tavily, Exa, Serper, arXiv, Semantic Scholar,
  OpenAlex, RSS, HuggingFace Hub, Lens.org (disabled by default), and
  an Upload adapter that re-exposes Inc 1's Upload through the unified
  Protocol.
- New package **`aleph-aiq`**: AIQ REST client (`dispatch_deep`, `get_job`,
  `cancel`, `clarify`, `health`); per-project AIQ YAML config generator
  that wires every `llms.*` block to `_type: openai` with
  `base_url=${LITELLM_BASE_URL}`; tokenomics adapter that turns AIQ
  PhaseStats into `ModelCall` + `CostLedgerEvent` rows
  (`purpose="aiq.<phase>"`, `actor_kind="aiq_agent"`); auth bridge with
  service-token issuance + verification (HS256, audience `aiq-server`,
  scope `aiq.full`, TTL ≤ 1h); `job_service` for AIQ ↔ AgentRun
  lifecycle.
- **Synthesis workflow** in `aleph_wiki.synthesis_workflow`: LangGraph
  DAG with `concept_normalize → citation_verification → wikilink_resolve
  → commit_revision → wiki_index_update`. Consumes an `AIQReport`
  structured object (body + sources + citations_by_marker + claims),
  emits draft wiki pages and `SynthesisProposal` rows. Citation
  verification failure blocks commit and raises
  `CitationVerificationFailure` with the missing markers listed.
- **`aleph_wiki.citation_verification`**: standalone implementation of
  AIQ's `verify_citations` + `sanitize_report` contract — drop-in
  replaceable with AIQ's once `vendor/aiq` is checked out. Extracts
  `[cN]` markers, looks them up in the source registry, raises on
  missing.
- **Alembic migration `inc3_aiq_synthesis`**: creates
  `connector_credentials`, `synthesis_proposals`, `approval_decisions`
  tables. Seeds the full Inc 3 connector roster including the disabled
  Lens.org row.
- **API routes (added under `/v1/projects/{id}/`):**
  - `/synthesize` — body `{topic, depth, allowed_connectors?}`. Creates
    AgentRun, issues service token, dispatches to AIQ. Returns
    `{agent_run_id, aiq_job_id, dispatched}`. If AIQ is not reachable
    the AgentRun is still recorded; the operator can retry once AIQ is
    up.
  - `/synthesis-proposals` — list, approve (flips proposal +
    `WikiPage.status` to `approved` in one transaction with an
    `ApprovalDecision` row), reject (proposal + page move to archived;
    `RejectionFeedback` written so next synthesis sees the reason).
  - `/connector-credentials` — owner-only list / put / delete / rotate.
    Plaintext never appears in any response or ledger payload.
  - `/internal/v1/aiq/credentials/{kind}`,
    `/internal/v1/aiq/sources`, `/internal/v1/aiq/model-calls`,
    `/internal/v1/aiq/events` — service-token gated callbacks. Wraps
    `register_uploaded_source` for the Source persistence path so AIQ
    sources go through the same ingestion pipeline as analyst uploads.

### Trace and ledger behavior added

- Ledger action kinds (new in Inc 3): `connector_credential.create`,
  `connector_credential.update`, `connector_credential.delete`,
  `synthesize.dispatch`, `synthesis.proposal.create`,
  `synthesis.proposal.approve`, `synthesis.proposal.reject`.
- OTEL spans: `synthesis.node.concept_normalize`,
  `synthesis.node.citation_verification`,
  `synthesis.node.wikilink_resolve`, `synthesis.node.commit_revision`.
  AIQ-side spans flow into Langfuse via the OTEL collector once
  AIQ is up.
- Cost ledger: every AIQ LLM call routes through the tokenomics
  callback and lands as a `ModelCall` + `CostLedgerEvent` with
  `actor_kind="aiq_agent"` and `purpose="aiq.<phase>"`.

### Known issues / debts (Inc 3-specific)

- **AIQ submodule not vendored in this session.** `vendor/aiq` is
  empty. Operators add the submodule via:
  ```bash
  TAG=$(gh release list -R NVIDIA-AI-Blueprints/aiq --limit 1 | awk '{print $1}')
  git submodule add -b $TAG https://github.com/NVIDIA-AI-Blueprints/aiq vendor/aiq
  ```
  Until then, `/synthesize` records the AgentRun in `pending`,
  `dispatched=false`. The proposal lifecycle and citation
  verification work without AIQ — the synthesis_workflow can be
  exercised by feeding a hand-built `AIQReport` directly.
- **Compose `aiq-server` service** is not added to
  `deploy/compose/docker-compose.yml`. Adding it depends on the
  submodule being in place; the runbook
  (`docs/operations/aiq-runbook.md`) lays out the operator steps.
- **No live-stack integration tests for AIQ** (test_aiq_server_health,
  test_synthesize_deep, test_aiq_writes_through_aleph, etc.) — these
  require the AIQ container to be running.
- **Frontend `SynthesizeButton`, `SynthesisProgressCard`,
  `SynthesisDraftPreview`** are not wired in this session. The
  approve/reject API endpoints are live and ledgered; the chat UI
  surfacing of the synthesis lifecycle lands alongside Inc 4's A2UI
  surfaces.
- **Eval datasets** (`synthesis_coverage.jsonl`,
  `citation_verification_recall.jsonl`, `connector_routing.jsonl`)
  not yet authored.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-4-a2ui-surfaces-design.md`.
Increment 4 wires A2UI: surface components for each right-panel tab
plus inline cards in chat. The BriefsSurface will replace Inc 3's
plain `GET /synthesis-proposals` JSON list with a proper A2UI-rendered
list with `ApprovalCard`s. The approve/reject API contracts from Inc 3
do not change; only their rendering.

---

## Increment 2: Wiki-first chat + assistant

**Completed:** 2026-05-27
**Commit range:** (will be filled at merge time)

### What was built

- New package `aleph-assistant` with `AssistantSession`,
  `AssistantThread`, and `AssistantMessage` models, the wiki-first
  retrieval router, the answer composer, and the LangGraph turn
  workflow.
- **`WikiFirstRetrievalRouter`:** FTS candidate generation via
  `IndexService.select_pages` → LLM page-selector
  (capability `page_selection`) tagging each pick as
  `primary`/`supporting`/`peripheral` → deterministic 1-hop wikilink
  expansion (bounded) → composer (capability `synthesis`) returning
  `{body_md, descent_requests, synthesis_requests}` → optional descent
  loop (LiteLLM embed of the inner query + `descend_into_source`
  hybrid-score against a single source's HNSW + FTS indexes) →
  re-compose. `coverage_judgment ∈ {ok, descent_used, descent_needed,
  synthesis_needed}` recorded on every result.
- **Assistant turn workflow (LangGraph DAG):** budget_gate →
  query_rewrite (deterministic deictic substitution from the last
  assistant message's first `[[wikilink]]`) → retrieve → finalize.
  Every node opens an OTEL span; every LLM call goes through
  `LiteLLMClient` so cost is ledgered.
- **API routes (`/v1/projects/{id}/...`):** sessions create/list/rename
  with `auto-create initial thread`, threads list + fork, messages
  list + post + get + SSE stream (`/messages/{id}/stream`),
  retrieval debug (`/retrieval/debug`, owner/editor only).
- **Worker:** `assistant_turn_job` verifies the agent token, loads
  prior messages + profile, runs the workflow, finalizes the AgentRun.
- **Alembic migration `inc2_assistant`:** creates `assistant_sessions`,
  `assistant_threads`, `assistant_messages` with the
  `uq_messages_thread_ord` constraint. No Inc 1 schema changes.
- **Web:** `ChatSurface` is now the center panel — auto-creates a
  session, posts user messages, polls for the in-progress assistant
  message, surfaces coverage judgment + cited wiki page titles +
  descent chunk counts on each bubble. Reuses `WikiBodyMarkdown` from
  Inc 1 for `[[wikilink]]` chips and `[cN]` markers.

### Trace and ledger behavior added

- Ledger action kinds (new in Inc 2):
  `assistant.session.create`, `assistant.session.rename`,
  `assistant.thread.fork`, `assistant.message.user_posted`,
  `assistant.message.complete`, `assistant.message.failed`,
  `assistant.message.budget_blocked`.
- OTEL spans: `assistant.turn`, `assistant.retrieve`,
  `assistant.page_selection` (via LiteLLM span),
  `assistant.compose` (via LiteLLM span), `litellm.embed` for
  `assistant.descent.query_embed`.
- Cost ledger covers every chat/embed call inside a turn.

### Known issues / debts

- **SSE streaming is approximate.** The assistant_turn workflow
  writes `body_md` in one shot at finalize; SSE streaming polls
  `body_md` length for incremental deltas. True composer token
  streaming lands in Inc 4 alongside the A2UI conversion.
- **No live-stack integration tests yet.** The pure-function tests
  for `_safe_json` and the existing chunking/wiki-service tests
  pass. The full §2.9 integration suite (chat_wiki_first,
  chat_descent, chat_synthesis_flag, chat_budget_*, chat_retry_fork,
  no_raw_chunk_rag) needs a running compose stack to be useful.
- **Eval datasets** (`page_selection.jsonl`, `descent_correctness.jsonl`,
  `citation_correctness.jsonl`, `synthesis_flag_precision.jsonl`) are
  not yet authored. The eval runner skeleton picks them up
  automatically once they're added.
- **Forking UI not wired.** The fork API endpoint is live; the
  per-message "retry from here" affordance lands in Inc 4.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-3-aiq-connectors-synthesize-design.md`.
Increment 3 vendors AIQ, rewrites its LLM configs to point at the
LiteLLM gateway, adds the full connector roster, and wires the
`/synthesize` action to the AIQ DeepResearcher so the wiki grows from
real queries. `RetrievalResult.synthesis_requests` is the contract Inc 3
consumes.

---

## Increment 1: RKS + intra-source retrieval + wiki skeleton

**Completed:** 2026-05-27
**Commit range:** (will be filled at merge time)

### What was built

- Two new Python packages: `aleph-rks` and `aleph-wiki`. Workspace
  manifest (`pyproject.toml`) updated; Alembic `env.py` imports both so
  their models register with the shared `Base.metadata`.
- **`aleph-rks`:** SQLAlchemy models for `Connector`, `ConnectorBinding`,
  `Source`, `SourceVersion`, `SourceAsset`, `NormalizedDocument`,
  `DocumentChunk` (pgvector + FTS), `RetrievalIndexRecord`. Sentence-aware
  markdown chunker (tiktoken-counted, overlap-carrying). Per-MIME
  normalization pipeline (pypdf primary + pdfminer fallback for PDFs;
  python-docx; readability-lxml + bs4 for HTML; ebooklib for EPUB;
  passthrough for MD/TXT). Batched embedding via `LiteLLMClient.embed`
  (`purpose="rks.embed"`). MinIO/S3 `AssetStore` with sha256 verify on
  read and presigned URL minting. Source registration service that
  writes Source + SourceVersion + SourceAsset atomically with ledger
  events.
- **`aleph-wiki`:** SQLAlchemy models for `WikiPage`, `WikiRevision`
  (immutable), `WikiSection`, `WikiLink`, `WikiClaim`, `Citation`,
  `SourcePage`, `Alias`, `HandEditMark`, `RejectionFeedback`, `WikiIndex`.
  `WikiService.commit_revision` — atomic, idempotent (no-op when body
  unchanged), hand-edit splicing (splices protected section text from
  the prior revision into the agent's proposed body before commit),
  ledger-event-per-revision, full claim + citation insert,
  WikiIndex refresh in the same transaction. `IndexService` for tsvector
  FTS over (title + aliases + summary) — the substrate Inc 2's LLM
  page-selector layers on top of. `AliasService` with `upsert`,
  `resolve`, and `repair_broken_links`. `HandEditMarkService` with
  `mark_section`, `clear_section`, `list_active_for_page`.
  `feedback_service` with `write_feedback`, `pending_for_concept`,
  `mark_addressed`.
- **Wiki ingest workflow (LangGraph DAG, 7 nodes):**
  concept_extraction → alias_extraction → source_page_compose →
  topic_page_stubs → wikilink_resolve → commit_revision →
  wiki_index_update. Prompts in `aleph_wiki/agent/prompts/` as
  versioned markdown files. Each node opens an OTEL span tagged with
  `aleph.node`, `aleph.agent_kind="wiki"`, project + source IDs. Hand-edit
  protection and rejection-feedback consumption are wired (feedback rows'
  `addressed_in_revision_id` is set on commit).
- **Alembic migration `inc1_rks_wiki`:** creates every Inc 1 table, the
  HNSW vector index on `document_chunks.embedding` (cosine), the GIN
  tsvector indexes on `document_chunks.text_tsv` and `wiki_index.index_tsv`,
  the immutability triggers on `wiki_revisions`, tsvector-maintenance
  triggers, and seeds the Upload connector row.
- **Worker jobs:** `normalize_job` (Source → NormalizedDocument; verifies
  sha256, dispatches by MIME, writes markdown to MinIO, enqueues
  downstream jobs, ledger event normalization.completed),
  `chunk_embed_job` (chunks + batched embed via gateway → DocumentChunk
  rows + RetrievalIndexRecord; budget rollups via the existing trigger),
  `wiki_ingest_job` (drives the LangGraph workflow, owns the AgentRun
  lifecycle including failure → wiki_failed). All jobs verify agent
  tokens; mint via the existing `/v1/agent-tokens` path.
- **API routes (8):** `/sources` (upload, list, detail, asset URL,
  normalized markdown), `/chunks` (list + intra-source search via the
  LiteLLM embed of the query against the HNSW index hybrid-scored with
  ts_rank), `/wiki` (pages, page detail with claims + wikilinks_out,
  by-slug, revisions, FTS search via `IndexService.select_pages`),
  `/handedits` (mark/clear), `/feedback` (write/list), `/aliases`
  (list/add/repair), `/connectors` (list + per-project bindings).
  Every state-changing route writes ledger events and is gated by the
  Inc 0 project-scope dependency.
- **Web tabs:** `SourcesTab` with upload modal and status badges (auto-
  refreshing as the worker progresses), `WikiTab` with page list +
  detail + lightweight markdown renderer (`WikiBodyMarkdown` renders
  `[[wikilink]]` chips and `[c12]` markers; clicking a chip navigates
  within the wiki). The Sources tab is added as a 6th right-panel tab
  for Inc 1 — it'll be folded into the WikiSurface's source-page filter
  view in Inc 4 alongside the A2UI conversion.
- **Unit tests:** chunking determinism, section_path preservation, empty
  edge cases (`aleph_rks/tests/test_chunking.py`); normalization
  dispatch and passthrough (`aleph_rks/tests/test_normalization.py`);
  wiki_service helpers — section splitting, slug, hand-edit splicing,
  hash (`aleph_wiki/tests/test_wiki_service.py`).

### Migrations added

- `inc1_rks_wiki` — every Inc 1 table; HNSW + GIN indexes; immutability
  triggers on `wiki_revisions`; tsvector-maintenance triggers for
  `document_chunks` and `wiki_index`; seeded Upload connector.

### Trace and ledger behavior added

- Ledger action kinds (new in Inc 1): `source.create`,
  `source_version.create`, `source.status_change`,
  `normalization.completed`, `embeddings.completed`,
  `wiki.revision.commit`, `wiki_ingest.succeeded`, `wiki_ingest.failed`,
  `connector_binding.create`, `connector_binding.update`.
- OTEL spans: `worker.normalize`, `worker.chunk_embed`,
  `worker.wiki_ingest`, `wiki.commit_revision`, and one
  `wiki.node.<name>` span per workflow node.
- Cost ledger covers all embedding calls (`purpose="rks.embed"`) and
  every wiki-agent LLM call (`wiki.concept_extraction`,
  `wiki.alias_extraction`, `wiki.source_page_compose`,
  `wiki.topic_page_stub`, plus the `rks.descent.query_embed` used by
  the intra-source search route).

### Known issues / debts

- **Live-stack validation pending.** The Inc 1 code compiles cleanly
  and the unit tests pass against the pure-function logic, but I have
  not run the full compose stack with a real MinIO / Postgres / gateway
  to validate the end-to-end upload→wiki path. The integration tests
  enumerated in §1.13 of the spec (upload-to-wiki, hand-edit
  preservation, rejection feedback consumed, embedder change reembed,
  failure path visible) are NOT yet written — they need the live stack
  to be useful and should land in the next session.
- **Eval datasets (`coverage_minimum.jsonl`, `alias_extraction.jsonl`)
  not yet authored.** The eval runner skeleton (Inc 0) will pick them
  up automatically once they're added; the gates in `--gate strict`
  are honored.
- **Web source-detail route not yet implemented.** The Sources tab
  surfaces sources and their status; clicking opens nothing in Inc 1.
  The detail page (normalized preview + chunk debug pane) lands next.
- **AssetStore in API process is best-effort.** If MinIO is unreachable
  at lifespan, the API still boots — uploads fail with a clear
  `validation_failed` problem detail. Production behavior should be
  to fail `/readyz`; this lands when the deployment story for the
  asset store firms up.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-2-wiki-first-chat-design.md`.
Increment 2 adds `AssistantThread`/`Message`/`Session`, the wiki
retrieval router (LLM page-selector layered on top of
`IndexService.select_pages` + 1-hop wikilink expansion + answer
composer), intra-source descent via the existing chunks/search
endpoint, the center-panel chat UI, and the cost banner enforcement
loop. No schema changes to Inc 1 entities are needed; if any are
required they'll go through a new Alembic migration.

---

## Increment 0: Foundations, ledger, cost spine, LiteLLM transport

**Completed:** 2026-05-27
**Commit range:** (will be filled at merge time)

### What was built

- Monorepo (`uv` + `pnpm` workspaces): `apps/api`, `apps/workers`, `apps/web`,
  plus six Python packages (`aleph-core`, `aleph-db`, `aleph-security`,
  `aleph-observability`, `aleph-models`, `aleph-evals`).
- Docker Compose stack: Postgres 18 with pgvector 0.8.2, MinIO,
  Redis 8.8, Langfuse 3.175, OTEL collector 0.153, plus the three
  Aleph apps.
- FastAPI app (`aleph-api`) with auth middleware (OIDC JWT + agent tokens),
  request-id middleware, error → RFC 7807 middleware, project-scope dep
  that returns 404 on non-membership.
- React 19 + Vite 8 + Tailwind 4 SPA with the 3-panel shell, project
  list, project create modal, cost banner, OIDC PKCE flow.
- Arq worker shell with a smoke job that round-trips a prompt through
  the gateway via an agent token.
- Alembic initial migration creating every Inc 0 table, the
  `pgvector` and `pgcrypto` extensions, the ledger-immutability triggers,
  the budget-rollup trigger, and the two seeded `ModelProfile` templates.
- `LiteLLMClient` (single chokepoint): chat + embed + health + list_models,
  tenacity retry, pricing-aware cost calc, ModelCall + CostLedgerEvent
  insert per call, Redis-backed idempotency cache, OTEL span per call.
- `LedgerWriter` with per-project chain head and sha256-chained events.
- `JWKSCache`, `verify_user_jwt`, `mint_agent_token`/`verify_agent_token`,
  `ProjectRole` + `require_at_least`.
- OTEL + Langfuse + structlog wiring with FastAPI / httpx / SQLAlchemy
  instrumentations.
- Bootstrap script (`scripts/bootstrap-local.sh`) and gateway verifier
  (`scripts/verify-gateway.sh`).
- CI workflow with lint, typecheck, unit tests, integration tests on a
  CI Postgres + Redis, eval skeleton, web build.

### Key files

- `pyproject.toml`, `pnpm-workspace.yaml`, `.gitignore`, `ruff.toml`/`pyrightconfig.json` (in `pyproject.toml`)
- `deploy/compose/docker-compose.yml`, `.env.example`, `otel-collector-config.yaml`
- `packages/aleph-core/src/aleph_core/{ids,time,errors,schemas/*}.py`
- `packages/aleph-db/src/aleph_db/{base,session,models/*,repos/*}.py`
- `packages/aleph-security/src/aleph_security/{principal,jwt,agent_token,roles}.py`
- `packages/aleph-observability/src/aleph_observability/{tracing,langfuse_client,logging}.py`
- `packages/aleph-models/src/aleph_models/{client,profile,pricing,retry}.py`
- `packages/aleph-evals/src/aleph_evals/{runner,cli}.py`
- `apps/api/src/aleph_api/{main,settings,lifespan,deps,middleware/*,routes/*}.py`
- `apps/api/alembic/versions/20260527_1200_inc0_initial.py`
- `apps/workers/src/aleph_workers/{settings,arq,jobs/smoketest}.py`
- `apps/web/src/{App,main,components/*,lib/*}.{ts,tsx}`
- `scripts/{bootstrap-local.sh,verify-gateway.sh}`
- `.github/workflows/{ci.yml,eval.yml}`

### Migrations added

- `inc0_initial` — every Inc 0 table, the `pgvector` + `pgcrypto`
  extensions, immutability triggers on `action_ledger_events`, budget
  rollup trigger on `cost_ledger_events`, two `ModelProfile` template
  rows.

### Tests added

- Unit:
  - `aleph-core`: UUIDv7 version/variant + monotonicity + deterministic seed (test_ids); ProjectCreate / ModelBindingIn / ModelProfileUpdate (test_schemas).
  - `aleph-security`: agent token round-trip, wrong-secret rejection, TTL bounds (test_agent_token); role gate behavior (test_roles).
  - `aleph-models`: pricing table coverage + cache discount math (test_pricing); profile resolver (test_profile).
  - `aleph-db`: hash chain determinism + canonical JSON sort (test_ledger_chain).
  - `aleph-evals`: runner skeleton — empty/missing root passes strict; dataset discovery (test_runner).
- Integration (`tests/e2e/`):
  - `test_project_lifecycle.py` — create project, verify 4 ledger events with chain continuity.
  - `test_ledger_immutable.py` — UPDATE and DELETE both raise.
  - `test_permission_leakage.py` — user B sees user A's project as 404.
  - `test_smoke_llm.py` — patched gateway round-trip writes ModelCall + CostLedgerEvent and updates budget.

### Trace and ledger behavior added

- Ledger action kinds covered: `user.create`, `project.create`,
  `project.update`, `project_member.add`, `project_member.remove`,
  `project_member.role_change`, `budget.set`,
  `model_profile.copy_from_template`, `model_profile.update`,
  `agent_run.create`.
- OTEL spans: `litellm.chat`, `litellm.embed`, plus auto-instrumented
  FastAPI request spans, httpx outbound spans, SQLAlchemy query spans.
- Cost ledger covers all LLM and embedding calls through the gateway
  (every chat/embed inserts a `ModelCall` + `CostLedgerEvent`).

### Manual verification

- [ ] `scripts/bootstrap-local.sh` succeeds on a clean clone.
- [ ] `alembic upgrade head` then `alembic check` returns clean.
- [ ] Web at http://localhost:5173 renders the 3-panel shell.
- [ ] OIDC login → `/v1/me` returns the resolved Principal.
- [ ] `POST /v1/projects` writes 4 ledger events and creates Project, ProjectMember, ModelProfile, Budget atomically.
- [ ] `POST /v1/projects/{id}/smoke/llm` returns a chat completion + cost + trace id.
- [ ] Langfuse UI shows the trace tagged with `aleph.project_id` and `aleph.purpose=inc0.smoke`.
- [ ] `UPDATE action_ledger_events` raises; `DELETE` raises.
- [ ] User B receives 404 (not 403) when reading user A's project resources.
- [ ] With the gateway unreachable, `/readyz` returns 503 with `litellm_gateway.ok=false`; project creation still works.

### Known issues / debts

- **Real OIDC IdP not bundled.** The compose stack does not include Keycloak.
  Local UI development against the API uses the test-only `verify_user_jwt`
  monkey-patch. Inc 0's acceptance criteria assume the operator provides
  an OIDC IdP (or sets up Keycloak separately).
- **`alembic check`** depends on every model decorator landing in
  `Base.metadata` at import time — guarded by importing
  `aleph_db.models` in `apps/api/alembic/env.py`.
- **Web app routing** uses a homegrown `parseRoute` shim. TanStack
  Router is included in the dep list and will replace this in Inc 1
  alongside real navigation.
- **Cost-per-token rates** in `pricing.py` reflect published list
  prices as of 2026-05-27; the gateway operator may have negotiated
  per-tenant pricing. Adjust the table when negotiated rates land.
- **No budget-edit API route in Inc 0.** Owner can mint a new project
  with a higher cap, or raise via SQL. A `PATCH /budgets` route lands
  in Inc 2.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-1-rks-wiki-skeleton-design.md`.
Increment 1 adds: RKS entities (`Source`, `SourceVersion`, `SourceAsset`,
`NormalizedDocument`, `DocumentChunk`), the Upload connector, the
normalization + chunking + embedding workers, the wiki entity skeleton
and the wiki ingest agent. Inc 1 introduces a new Alembic migration —
never edit `inc0_initial`.

---

## Session 2026-05-28/29 — get-it-running, research pipeline, conversational UX, Waves 1/2/5

This session took the stack from "boots but severely broken" to a coherent,
working product. Not increment-numbered; tracked as Waves (one commit each).
Commits are on `main` (`adcca40` → `84d58f1`). Full source/MCP references at
the end so future sessions know where to look.

### What shipped (by commit)

- **Wave 1** (`dd6ebd6`, `11fa913`) — progress visibility (`AgentEvent` per
  workflow node + SSE stream + Activity card at top of chat), design tokens +
  dark/light theme, real Wiki page browser/reader, working theme toggle.
- **Wave 2 backend** (`226f123`) — assistant **Deep Agent** over AG-UI:
  `apps/api/src/aleph_api/copilot_agent.py` (`create_deep_agent` +
  `search_wiki` tool + `CopilotKitMiddleware`), mounted via
  `ag_ui_langgraph.add_langgraph_fastapi_endpoint` (the v1
  `CopilotKitRemoteEndpoint` path is broken against current `ag-ui-langgraph` —
  `dict_repr` AttributeError — so we use the v2 path). Project scope rides the
  `proj:<uuid>:<thread>` thread-id (the only channel `ag-ui-langgraph` threads
  into `config.configurable`; `forwarded_props` go into graph *state*, not
  config — verified in the installed package). `_active_ctx` module globals →
  `ContextVar` (W2.1).
- **Wave 2 runtime + frontend** (`1723da4`) — new **`aleph-copilot-runtime`**
  Node service (`apps/copilot-runtime/`, compose service on :4000):
  `@copilotkit/runtime` v2 `CopilotRuntime` + `HttpAgent` → the FastAPI AG-UI
  endpoint, with `a2ui.injectA2UITool` + an **inline "aleph" catalog**
  (so the agent stamps `catalogId:"aleph"` and emits Aleph cards). Frontend:
  `CopilotKitProvider` (`lib/copilot.tsx`) + `createA2UIMessageRenderer({catalog})`
  adapting the 17 Aleph cards (`a2ui/copilot-catalog.tsx`), `CopilotChatSurface`
  with `useAgentContext` (agent reads active tab) + `useFrontendTool`
  (`open_surface` drives the right panel), Live/Classic toggle. Verified
  in-browser: shared state both directions + agent renders a real ChartCard
  (Vega) inline. **Both Dockerfiles use `npm` not `pnpm`** (pnpm 10 hard-fails
  on blocked esbuild/@scarf build scripts).
- **Connector seeding** (`49a3384`) — project creation seeds `ConnectorBinding`
  rows (no-auth defaults enabled, auth ones bound-disabled) so research works
  with zero manual setup. Fixed `/synthesize` 422 "no connectors enabled".
- **AIQ dispatch** (`8f783c6`, `0eddb35`, `38b27a4`) — see the dedicated memory
  `project_aiq_research_pipeline.md`. Job-store schema vendored
  (`deploy/compose/aiq-init-{jobs,checkpoints}.sql`) + applied in
  `bootstrap-local.sh`; `AIQClient` rewritten to the real API; image bumped
  `2.0.0 → 2.1.0` (2.0.0 had an `orchestrator_llm` crash). Web search wired
  (`aiq-config-default.yml` data_source_registry + tavily; `TAVILY_API_KEY`).
- **Research → wiki** (`4735cb8`, `e8d9f3e`) — `aiq_synthesis_poll_job`
  (aleph-workers) polls the AIQ job, parses the report into the `AIQReport`
  the orphaned `synthesis_workflow` expects (remaps `[N]`→`[cN]` citation
  markers), runs it → draft wiki page + Briefs proposal. `/synthesize` enqueues
  it. Poller refactored to **re-enqueue-with-defer** (no worker held through
  long deep research; 30-min ceiling).
- **Conversational research** (`17b75a8`) — Live agent gains a `start_research`
  tool (self-calls `/synthesize`); "research X" kicks off research as a
  background card.
- **Branding + theme** (`f3e2e3b`, `e8d9f3e`) — Aleph/א wordmark
  (`components/AlephLogo.tsx`) on the landing page + theme toggle; Live chat
  themed dark (CopilotKit `.dark` class mirrored from `data-theme`, serif
  prose, inline-style light boxes repainted).
- **Wave 5** (`0bdde5f`, `84d58f1`) — analyst UX: ACH matrix
  (`GET /hypotheses/ach` + `HypothesisMatrix.tsx`, Heuer's fewest-disconfirming),
  Notes editor + `POST /notes/{id}/promote` (→ draft page + Briefs proposal),
  readable sources (SourceCard "Read" expander → `/sources/{id}/normalized`).

### Infrastructure touched OUTSIDE this repo
- **`~/code/ARLIS/insights-k8s-manifests`** (the Insights LiteLLM gateway,
  GitOps via ArgoCD): added per-model `additional_drop_params:
  ["parallel_tool_calls"]` to `litellm/proxy-config.yaml` (commits `05342ac`,
  `0c4cf46` on `main`). Root cause: LangChain/AIQ send `parallel_tool_calls`,
  LiteLLM mis-folds it into a malformed Bedrock `tool_choice` → 400
  "tool_choice.type: Field required". `drop_params` alone and the global
  `litellm_settings` form do NOT drop it — must be per-model `litellm_params`.
  Verified live. **Known issue:** that app's ArgoCD sync is wedged on a broken
  `PostSync` hook (`bootstrap-teams` can't pull `ghcr.io/curlimages/curl` —
  403); applied the fix live via `kubectl patch configmap` + `rollout restart`.
  Future pushes won't auto-deploy until the hook image is fixed.

### Deferred
- **Wave 3** (Deep Agents harness) — `2026-05-29-wave-3-deep-agents-design.md`.
  Lower value than planned (agents already decompose work).
- **Wave 4** (A2UI v0.9 protocol) — `2026-05-29-wave-4-a2ui-v09-design.md`.
- **Cost-attribution gap:** ChatOpenAI (agent path) bypasses `LiteLLMClient`
  so doesn't write `ModelCall`/`CostLedgerEvent` (rule #5). Langchain callback
  handler is the planned fix.
- **Deep research duration** can exceed snappy expectations; agent defaults to
  shallow. The poller now survives long runs (re-enqueue) up to 30 min.

### Reference map — where to look for correct docs/planning
- **Local repos:** `~/code/aiq` (NVIDIA AI-Q Blueprint **v2.1.0** source —
  matches the deployed image; the source of truth for AIQ config `_type`
  values, agent tool auto-inheritance, job-store schema `init-db.sql`, and the
  real HTTP API). `~/code/A2UI` (A2UI core + React renderer + Python SDK —
  W4 reference). `~/code/obsidian-llm-wiki-local` (retrieval / hand-edit /
  rejection-feedback / alias patterns). `~/code/ARLIS/open-analyst`
  (supervisor+subagent / WriteAuthorityMiddleware patterns for W3; ActivityCard
  / HypothesisMatrix / NotesTab UI patterns). `~/code/ARLIS/insights-k8s-manifests`
  (the gateway deployment — LiteLLM `proxy-config.yaml`, model list, all
  `bedrock/global.anthropic.*`, LiteLLM `main-v1.83.14-stable`).
- **A2UI website:** `a2ui.org` + the v0.9 spec/catalog JSON it links — protocol
  source for W4.
- **MCP servers used this session:**
  - `copilotkit-mcp` (`search-docs`/`explore-docs`/`search-ag-ui-docs`) —
    CopilotKit + AG-UI docs. **Caveat:** docs were ahead of the published 1.58
    package; always cross-check against `npm pack` `dist/*.d.mts`.
  - `context7` (`resolve-library-id` → `query-docs`) — used to confirm LiteLLM
    `additional_drop_params` belongs in per-model `litellm_params`.
  - `plugin_playwright_playwright` — drove the live UI for in-browser
    verification (the standing per-wave requirement).
- **Skills available for the deferred waves:** `framework-selection`,
  `deep-agents-core`, `deep-agents-orchestration`, `deep-agents-memory`,
  `langgraph-*`, `langchain-middleware` (W3); these document the installed
  `deepagents 0.6.6` / `langgraph >=1.2,<2` APIs authoritatively.
- **Key API-probe technique** (reused throughout): for any moving dep, don't
  trust memory or even the MCP docs — `npm pack <pkg>@<ver>` then read
  `dist/*.d.mts`, or `uv run python -c "import x; print(dir(x))"`. This is how
  the CopilotKit v2, a2ui-renderer, ag-ui-langgraph, and AIQ APIs were pinned.

---

## Wave 6 — Complete the conversational pivot: Live is the only surface

**Completed:** 2026-05-29. Branch `wave-6-conversational-completion` (12 task
commits `5c93dbf`→`e2891f1` + chat-routing fix `d75b69b`). Spec/plan:
`docs/superpowers/specs/2026-05-29-wave-6-conversational-completion-design.md`,
`docs/superpowers/plans/2026-05-29-wave-6-conversational-completion.md`.

### What shipped
- **Agent tool suite** on the Live Deep Agent (`apps/api/src/aleph_api/copilot_agent.py`):
  `read_wiki` (wraps the full `WikiFirstRetrievalRouter` — deep cited retrieval,
  reaching Classic's depth), `list_hypotheses_tool`/`create_hypothesis_tool`/
  `add_hypothesis_evidence_tool` (+ shared `_dev_principal`, stable
  `_DEV_USER_UUID`), `ingest_source` (+ new `POST /sources/ingest-url` route),
  `build_artifact` (+ `ArtifactCard` added to runtime + frontend + canonical +
  right-panel catalogs), `list_connectors`/`set_connector_enabled`/
  `set_model_profile`. Tools self-call tested routes (`Bearer local-dev`).
- **Approval gating (Phase B):** native `create_deep_agent(interrupt_on=...)` was
  probed and does **NOT** surface an approval UI in this CopilotKit v2 +
  ag-ui-langgraph stack (the gated tool just executed). Fell back to:
  consequential tools (`build_artifact`, `set_connector_enabled`) create a
  pending `ApprovalRequest(target_kind="agent_action", proposed_patch_jsonb=
  {tool,args})` via `POST /agent-actions/request` and render an `ApprovalCard`;
  the `_approve`/`_reject` handlers (`a2ui_handlers.py`) allowlist-dispatch the
  stored action (`with_for_update()` guards against double-approve), execute via
  self-call, write `ApprovalDecision` + `approval_request.approved/rejected`
  ledger. **Chat-routing fix (`d75b69b`):** the Live-chat card adapter
  (`copilot-catalog.tsx`) was dispatching `onAction` back into the agent stream
  instead of POSTing to the `ActionRouter` — so chat ApprovalCard clicks never
  executed. Now posts to `/cards/actions` like the right panel. Verified
  in-browser: build → ApprovalCard → Approve → artifact built + request approved
  + ledgered.
- **Agent cost attribution (Phase C, rule #5):** `AgentCostCallbackHandler`
  (`copilot_cost_callback.py`) writes `ModelCall`+`CostLedgerEvent` for the
  agent's `ChatOpenAI` turns (mirrors `CostWriter.record_call`/`pricing`),
  resolves project from the `proj:<uuid>` thread-id, bounded `_pending`, never
  crashes the turn. **Gotcha:** streaming drops usage unless `stream_usage=True`
  on `ChatOpenAI` — set it. No double-count (callback only on the agent model).
  Verified live: an `assistant.turn` row ($0.0273, 1 call) shows in Profile→Usage.
- **Cross-session memory (Phase D):** langgraph `AsyncPostgresStore` (deps
  `langgraph-checkpoint-postgres`, `psycopg[binary,pool]`) opened in the lifespan
  (agent build moved into lifespan — store needs a running loop), `CompositeBackend`
  routing `/memories/`→`StoreBackend`, **per-project namespace** derived from the
  thread-id (`(project_uuid,"memories")`, fallback `("shared","memories")`).
  `store.setup()` creates `store`/`store_migrations` tables.
- **Cost UI + bell (Phase E):** cost removed from the top `CostBanner` (deleted)
  and folded into a **Usage** section in the Profile drawer (`Drawers.tsx`,
  bottom-left ● button); notification bell glyph → monochrome SVG matching its
  siblings (`LeftPanel.tsx`).
- **Retire Classic (Phase F):** default `chatMode="live"`, toggle removed,
  `ChatSurface.tsx` deleted. `assistant_turn_job` + `WikiFirstRetrievalRouter`
  kept (reused by `read_wiki`; the worker job is now UI-orphaned but retained).
- **e2e conftest fixed (in A6):** `ALEPH_ENV=ci`→`local` (Settings rejected "ci")
  and the lifespan is now actually driven (`app.router.lifespan_context`) +
  MinIO env — the whole integration suite was previously broken at setup. New
  ledger-assertion integration tests for the mutating tools.

### Honest gaps / known issues
- **Pre-existing `alembic check` drift** (on `budgets`/`cost_ledger_events`/
  `ledger_chain_heads` index flags) exists identically on pre-Wave-6 commits —
  NOT introduced here, but it will fail CI's `alembic check` gate until fixed.
- **Pre-existing unit failure** `aleph-evals/test_runner.py::test_discovers_dataset_dirs_with_manifest`
  (confirmed on clean HEAD; unrelated to Wave 6).
- **`Bearer local-dev` self-call** is local-auth-mode only (consistent with all
  agent tools); would need a real agent token under OIDC.
- **D1 cross-session memory** verified by construction (store tables, per-project
  namespace, clean startup) but NOT yet by a full write-then-read-across-sessions
  browser run.
- Builder Inc-7 debts unchanged (chart-PNG container, DOCX, bundled CSL).
- The `*.id.hex[:8]` correlation_id collision pattern (fixed in the ingest path
  in A6) still exists in `artifacts.py`/`assistant.py`/aleph-aiq/workers.

### Operational note (cost a verification cycle)
`aleph-api` and `aleph-web` run from **baked images with no bind mount / no
`--reload`** — code changes need `docker compose up -d --build <svc>`, NOT
`restart`. Several tasks initially used `restart` (insufficient); a full
`up -d --build` makes the running stack match HEAD.

---

## Wave 4 — A2UI v0_9 shared catalog + delta updates

**Completed:** 2026-05-29. Merged to main (`34a4b2a`); branch `wave-4-a2ui-v09` (T1–T7 + final-review fix). Refreshed spec: `2026-05-29-wave-4-a2ui-v09-refresh-design.md` (supersedes the stale one); plan: `2026-05-29-wave-4-a2ui-v09.md`.

### What shipped (spike-gated, each task browser-verified)
- **Shared v0_9 catalog** (`apps/web/src/a2ui/aleph-catalog-v09.tsx`): the 18 Aleph components defined ONCE via `@a2ui/react/v0_9` `createComponentImplementation`, composed into `new Catalog("aleph://v1", ...)`. Both the right panel and the Live chat consume it (the duplicate `copilot-catalog.tsx` was DELETED; CopilotKit's `createA2UIMessageRenderer` takes the upstream `Catalog` directly).
- **Panel renderer** (`A2UISurfaceView` + `A2UIStreamSurfaceView`): renders via `MessageProcessor` + `<A2uiSurface>`; the homegrown `register.tsx` walk-and-dispatch is RETIRED (context re-homed to `surface-context.tsx`, which also keeps `renderChildCard` for Wiki/Briefs embedded children the binder doesn't walk).
- **Backend v0_9 messages** (`aleph_a2ui/messages.py`): `create_surface`/`update_components`/`update_data_model`/`full_surface` (nested-envelope wire shape `{version:"v0.9", <kind>:{...}}`). All 5 tabs' builders emit v0_9; the surface ROOT component must have `id="root"` (the renderer hard-codes that lookup).
- **Delta SurfaceStreamer** (`aleph_a2ui/surface_streamer.py` + SSE `GET /surfaces/{tab}/stream`): full surface on connect, then `diff_data_model` minimal patches emitted as `updateDataModel` deltas on a 2.5s per-connection recompute-and-diff. Verified live: creating a hypothesis makes its card appear via a delta with no reload.
- **Unified card actions**: the shared catalog's `adapt()` POSTs `{action_kind,target_id,target_kind,params}` to `/cards/actions` (ActionRouter) for BOTH chat and panel.

### Load-bearing finding (carry forward)
- **The v0_9 Generic Binder reads zod-v3 internals.** Card schemas MUST use the `zod3` alias (`"zod3":"npm:zod@3.25.76"` in `apps/web/package.json`); an app-zod-4 `z.object` schema SILENTLY disables binding (renders raw `{path}` → React crash). All 18 card schemas use `z3.object`.

### Honest scope / gaps
- **Deltas are meaningful only for the Hypotheses tab.** The other 4 tabs (Wiki/Artifacts/Notes/Briefs) render as single self-fetching surface views (data NOT in the A2UI data model), so they emit the full surface once + idle (self-refresh via react-query). The general streamer is built so future bound tabs benefit for free.
- **Runtime agent-facing catalog** (`copilot-runtime/src/server.ts`) still advertises only 7 cards to the agent (Chart/Table/Claim/Hypothesis/Finding/Source/Artifact) — ApprovalCard etc. aren't advertised, so the agent rendering an ApprovalCard inline is LLM-flaky. The RENDER catalog (frontend) has all 18; this is just the agent-prompt schema. Out of scope for W4.
- **SSE auth:** EventSource can't send Authorization headers → relies on `local` auth mode (same as the existing agent-events stream); OIDC would need a cookie/query-token path.
- **Per-connection 2.5s polling** = N viewers → N recompute loops; fine for single-user local, would want LISTEN/NOTIFY at scale.

### Operational note
`aleph-web` / `aleph-api` are baked images (no bind mount) — every change needs `docker compose up -d --build <svc>`. A transient npm-registry hiccup once made a web rebuild fail (`@copilotkit/web-inspector`); it resolved on retry. `Dockerfile.dev` does a lockfile-free `npm install` (non-reproducible) — a latent fragility worth a lockfile.

---

## Wave 3 (reconsidered) — Orchestrator + purpose-built subagents

**Completed:** 2026-05-29. Merged to main (`410e0d8`); branch `wave-3-orchestrator-subagents` (T1–T8 + final-review fix). Refreshed spec `2026-05-29-wave-3-orchestrator-subagents-refresh-design.md` (supersedes the stale `2026-05-29-wave-3-deep-agents-design.md`); plan `2026-05-29-wave-3-orchestrator-subagents.md`. NOTE: the original W3 spec ("migrate editorial/wiki LangGraph agents to the harness") was correctly rejected as low-value framework-churn; this reconsidered wave unlocks the harness's REAL value — context isolation + planning + delegation.

### What shipped (each subagent verified following the live-verified exemplar)
- **Orchestrator** (`apps/api/src/aleph_api/copilot_agent.py`): the Live assistant is now a thin Deep-Agents orchestrator. Heavy inline tools DRY-extracted into module-level `_read_wiki_impl`/`_start_research_impl`/`_ingest_source_impl`/`_build_artifact_impl`/`_{list,create,add_evidence}_hypothesis*_impl`; inline tools removed. `_gateway_chat_model(settings,*,purpose)` + `subagent_model(settings,name)` give each subagent a gateway model with a per-subagent cost callback (`assistant.subagent.<name>` — rule #5, verified: retriever rows in `model_calls`). Lean orchestrator `tools=` (search_wiki + connectors/profile). SYSTEM_PROMPT rewritten to plan-and-delegate.
- **6 subagents** (`apps/api/src/aleph_api/subagents/*.py`), each `build_<name>_subagent(*, settings) -> SubAgent` WRAPPING existing services (function-local imports break the cycle): **retriever** (deep `WikiFirstRetrievalRouter` read — isolates the large body), **researcher** (delegates the AIQ `/synthesize` arc), **wiki_builder** (ingest_source + note-promote), **viz_builder** (`make_chart`→ChartCard + approval-gated build_artifact), **analyst** (hypotheses + ACH), **reviewer** (new `POST /reviews/editorial` → enqueues the existing project-scoped `editorial_review_job`).
- **Skills** (`apps/api/src/aleph_api/skills/{research,ach,report-authoring,wiki-style}/SKILL.md`): progressive disclosure. Wired via a `FilesystemBackend(virtual_mode=True)` routed at `/skills/` in the CompositeBackend (key finding: SkillsMiddleware reads through the agent's BACKEND, not the host FS).
- **Plan legibility** (`ActivityCard.tsx`): reads `useAgent("assistant").state.todos` (CopilotKit v2 `useAgent` + `UseAgentUpdate.OnStateChanged`) and renders the orchestrator's live `write_todos` plan with per-item status. Hides when empty.

### Verified live (browser)
A multi-step request → the orchestrator delegated via the `task` tool to **retriever** (returned a distilled cited answer; the composed body stayed out of the main thread) and **analyst** (created hypothesis "Example Domain is reserved…" with 4 evidence, rendered in the Hypotheses tab via the Wave-4 v0_9 surface); the agent explicitly **"checked memories" and "read the research skill"** (memory + skills engaged); subagent cost rows confirmed in `model_calls`.

### Honest scope / findings
- **deepagents `AsyncSubAgent` = a remote hosted LangGraph graph** (`{name, graph_id, url}`), NOT in-process — out of scope (would need a LangGraph Server deployment). Long-running work (research, big ingest) uses the EXISTING arq job pipeline, which already gives "kick off, keep talking, progress in Activity." In-process **sync `SubAgent`s via the `task` tool deliver the core value** (context isolation).
- **`write_todos` is LLM-discretionary** — the Activity Plan section is wired + crash-safe, but the agent only populates it when it chooses to plan with todos (it delegated directly for a simple 2-step task in testing). Correct behavior; the read path is verified.
- **editorial reviewer is project-scoped** (the worker scans all pages); `reviewer`'s page title is carried as the review `trigger` label, not a per-page filter. A true single-page review would need a workflow change.
- Pre-existing `aleph-evals/test_runner.py` unit failure persists (not Wave 3); `alembic check` clean (the new route added no model change).

---

## CI pipeline fix + agent-surface test coverage (2026-05-29)

**Completed:** 2026-05-29. Pushed direct to `main` (`674d46c`, `aacb279`, `a5aa2e0`).
Closes the "CI is red" reality from `system-assessment.md` and the P2 test-coverage
gap. After this, **all five CI jobs are green on `main`** (lint-and-typecheck,
unit-tests, integration-tests, evals, build-web).

### CI pipeline — root causes + fixes
- **`uv sync --all-extras` (all 4 jobs) → workspace members uninstalled** → pyright
  `reportMissingImports`, import failures. Fixed to `uv sync --all-packages --all-extras`
  everywhere (`674d46c`). This is the same gotcha CLAUDE.md already records for local setup;
  CI had the wrong flag.
- **`ALEPH_ENV: ci`** is not a valid `Settings.aleph_env` (`Literal["local","dev","staging","prod"]`)
  → `ValidationError` at app construction. Set to `local`.
- **integration-tests had no MinIO** → the lifespan couldn't build an `AssetStore`, so
  `test_ingest_url_*` 422'd with "asset store is not configured". Start MinIO as a
  `docker run` step (GH `services:` can't pass MinIO's `server /data` command), wait on
  its health endpoint, and supply `MINIO_ENDPOINT`/`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`/
  `ALEPH_S3_BUCKET`. The `aleph-local` bucket auto-creates on first use (`aacb279`).
- **`test_permission_leakage` asserted 404, got 200.** It monkeypatches `verify_user_jwt`,
  which is only called in **oidc** mode; under CI's `local` auth mode the middleware
  collapses every request to the single dev principal, so users A and B were the same and
  the isolation check was vacuous. Gave the test a dedicated oidc-mode app/client fixture
  (`ALEPH_AUTH_MODE=oidc` + `get_settings.cache_clear()`, restored on teardown) so it
  exercises the real cross-principal isolation path; `verify_user_jwt` is patched
  per-identity so the `JWKSCache` is never hit over the network (`aacb279`).

### Agent-surface test coverage (P2 #8 — was browser-verified-only)
- **SSE delta pipeline (pure, the brittle part):** `split_surface_messages` +
  `data_model_patches_to_messages` (incl. the array-remove "re-set whole array" fallback),
  plus a `diff_data_model`→messages round-trip for a real hypotheses delta.
  (`packages/aleph-a2ui/tests/test_surface_streamer.py`)
- **Subagent delegation wiring (deterministic, no LLM/DB):** builds all six subagents and
  asserts `SubAgent` shape + per-subagent cost tag (`assistant.subagent.<name>`, rule #5)
  + gateway `base_url` (rule #2). (`apps/api/tests/unit/test_subagents.py`)
- **Card builders:** `ClaimCard`/`SourceCard`/`ApprovalCard` type/props/id behaviour.
  (`packages/aleph-a2ui/tests/test_cards.py`)
- **agent-events integration:** list-endpoint serialization (the same query the SSE
  `/stream` poll runs) + cross-project scoping. (`tests/e2e/test_agent_events.py`)
- Counts: unit **69 → 97**, integration **12 → 14**.

### Honest scope / what's still uncovered
- The SSE **timer-loop** itself (the 2.5s per-connection recompute that wires the pure
  diff pieces over a live connection) is still only browser-verified; its pure components
  — where the logic lives — are now unit-covered.
- A **full orchestrator→subagent delegation** run (the LLM actually invoking the `task`
  tool) is still browser-verified only; it's inherently non-deterministic and unsuitable
  for CI. The wiring it depends on (subagents built + cost-tagged) is unit-covered.

---

## Wave: Real-time push layer + Live Wiki (2026-05-30)

**Branch:** `wave-realtime-push-live-wiki`. Spec:
`docs/superpowers/specs/2026-05-29-live-wiki-design.md` (scope expanded to all streams).
Product enrichment toward the "living KB" vision: the wiki tab updates the instant an
agent writes, with full presence ("✦ agent is editing this page…" + "updated just now"
pulse), built on a new push layer that replaces idle polling under **every** SSE stream.

### Push layer (foundation)
- **Migration `realtime_notify_triggers`:** `AFTER INSERT/UPDATE` `pg_notify('aleph_changes',…)`
  triggers on `action_ledger_events`, `agent_events` (project resolved via `agent_runs`),
  `assistant_messages`. Delivered on COMMIT; identity-only payloads. (one statement per
  `op.execute` — asyncpg's extended protocol rejects multi-statement strings.)
- **`apps/api/.../realtime.py`:** `ChangeBroker` (in-process per-project fan-out, pure) +
  `NotifyListener` (one supervised asyncpg `LISTEN` connection; event-driven reconnect via
  `add_termination_listener`, no polling). `asyncpg_dsn()` strips the `+asyncpg` tag. Built +
  started/stopped in the lifespan.
- **Design choice:** triggers over Redis pub/sub — automatic (can't be bypassed by any code
  path) + transactionally correct (after-commit). Each stream keeps a **slow poll fallback**
  under the push so a dropped listener self-heals (push primary, fallback safety net — not a
  shortcut, the production-grade way to do push).

### Streams migrated (all four)
agent-events (5s fallback), surfaces recompute-and-diff (10s), assistant message tail (1s),
and the new changes stream (5s). Each subscribes to the broker and wakes on push; the
cursor/recompute logic is unchanged, so nothing is ever missed regardless of signal timing.

### Live wiki (consumer)
- **`routes/changes.py`** `GET /changes/stream` — push-native; emits `committed` (ledger
  allowlist `{wiki.revision.commit}`) / `compiling` / `compile_done` (the page-scoped
  `compile_page` phase). Pure serializers; 60s replay on connect for in-flight presence.
- **Wiki workflow:** emits `compile_page` phase_started at source-page compose (lasting
  presence through the multi-second LLM compose) + compile_done after commit; the
  `wiki.revision.commit` ledger payload gains `page_title`.
- **Frontend:** `useWikiLiveSignals` subscribes to the stream, maintains compiling/recently-
  committed maps, and invalidates the `wiki-pages` + `wiki-page` queries on commit so the
  index AND the open page refresh in place (fixes the open page never refreshing). The index
  poll dropped 4–15s → 30s backstop. `WikiSurface` renders the "✦ editing…" badge, "updated"
  pulse, reader banner, and header "building…" hint.

### Tests (thorough)
- **Unit (113 total, +16):** ChangeBroker fan-out/scoping/timeout/drain/full-queue-drop +
  `asyncpg_dsn`; the changes serializers (allowlist, page-id requirement, shapes, ordering).
- **Integration (24 total, +10):** end-to-end push (ledger & agent_event writes → broker,
  scoped); each migrated stream **wakes on push** (<3s, << its fallback) **and self-heals via
  fallback** (listener stopped); surfaces emits a real v0.9 hypotheses delta; assistant pushes
  a token delta; changes stream emits committed (on a wiki commit) + compiling (on compile_page).
  - Testing note: httpx **ASGITransport can't stream** (runs the app to completion), so the
    stream tests drive `StreamingResponse.body_iterator` via a background **pump task + queue**
    — `wait_for(anext(agen))` would cancel the in-flight `__anext__` and destroy the generator.
- web tsc + eslint + build clean.

### Honest scope / limits (unchanged from spec)
- Push primary + **poll fallback** (1–10s safety nets, not the latency you see).
- One listener connection per API process (single uvicorn process today).
- `agent_events` trigger does a PK lookup on `agent_runs` for project_id (no schema change).
- **Hand-edits** write no ledger event → not live (out of scope; editor sees their own edit).
- **SSE × OIDC** unchanged — all streams (this included) rely on `local` auth mode; documented.
- The `changes` stream is general (allowlist) — Notes/Briefs can consume it later.

## Host-memory protection: AIQ in-flight gate + compose memory caps (2026-06-12)

**Context:** On 2026-06-11 the EC2 host (16 GiB / 4 vCPU) died of swap thrash —
unclean reboot + ext4 recovery. Root cause: the Playwright suite created 7
projects; each auto-bootstrap fanned out 3 AIQ shallow-research jobs; all 21
ran concurrently as Dask workloads inside the single aiq-server container.
No compose service had a memory limit, so the kernel paged the host to death
before the OOM killer could act. Bootstrap-on-create stays ON — the fix is
admission control + hard caps, not feature removal.

### What was built

- **`aleph_aiq.throttle.AIQThrottle`** — Redis sorted-set admission gate
  bounding research jobs in flight inside aiq-server, shared by every
  submitter (API `/synthesize` + workers). Rank-based acquire (never
  over-admits under concurrency); TTL prune (45 min > the poller's 30-min
  ceiling) self-heals slots leaked by a crashed poller.
- **`_dispatch_core` gating** — with a free slot, inline dispatch exactly as
  before; with the gate full, the submission is handed to the new deferred
  **`aiq_submit_job`** (15s defer, 120 attempts ≈ 30 min max queue wait,
  within the poll token's 1h TTL). The run is queued, never dropped; ledger
  records `aiq.job.queued` vs `aiq.job.dispatched`. `StartedResearch` gains
  `queued`.
- **`aiq_submit_job`** (workers) — retries the gate; acquired → dispatch +
  enqueue poll job; full / AIQ briefly down → requeue itself; exhausted →
  AgentRun failed + `aiq.dispatch.failed`. Registered in arq.
- **Slot release** in `aiq_synthesis_poll_job` on every exit from AIQ: any
  terminal status and the poll-timeout give-up.
- **Settings:** `AIQ_MAX_CONCURRENT_JOBS` (default 3; API + workers),
  `ARQ_MAX_JOBS` (default 10, env-overridable; compose sets 4 locally).
- **Compose memory caps** on all 10 services (`mem_limit` +
  `memswap_limit` equal = container swap OFF, so a runaway is cgroup-OOM-
  killed + restarted instead of thrashing the host): aiq-server 4g, workers
  2g, api/web/postgres 1.5g, langfuse 1g, minio/copilot-runtime 512m,
  redis/otel 256m. Caps total ~13g on the 16g host.

### Tests
- Unit (+20): throttle semantics (admit/reject/release/idempotent
  re-acquire/TTL prune); dispatch-core gating (slot held, gate-full defers,
  queued holds no slot); submit-core (dispatch/requeue/exhaust/health-blip
  releases); `aiq_submit_job` wrapper (event + status writes); poll job slot
  release (terminal + timeout, held while running); settings env overrides.

### Honest scope / limits
- The gate bounds **AIQ research jobs** only; other worker jobs are bounded
  by `ARQ_MAX_JOBS`. aiq-server's own 4g cap is the backstop.
- Rank-based acquire assumes one host clock (true locally: all containers
  share the kernel clock). Cross-node deploys should keep the same gate via
  shared Redis — clock skew can reorder fairness but the TTL prune bounds any
  over-admission window.
- If a poll job crashes hard (not timeout), its slot frees only via the
  45-min TTL prune.

## Audit remediation — Phase 1: ledger holes + chain verify (2026-06-30)

Branch `audit-remediation`. Closes audit findings F08 (rule-#4 ledger holes) and
F09 (no runtime chain verification). See
`docs/superpowers/plans/2026-06-30-audit-remediation-acceptance.md`.

### What shipped
- **Runtime chain verification.** `verify_event_chain` (pure, recomputes the
  sha256 chain over a sequence of events — unit-tested for intact/tampered/empty)
  + `verify_project_chain` (loads a project's events in chain order) in
  `aleph_db.repos.ledger`; exposed at `GET /v1/projects/{id}/ledger/verify`
  (`{ok, count, first_divergence_event_id}`). Tamper detection is unit-tested on
  hand-built events (the Postgres immutability triggers block real-row tampering).
- **Closed the four ledger holes.** `AliasService.upsert` → `wiki.alias.upsert`;
  `AliasService.repair_broken_links` → `wiki.links.repair` (only when ≥1 repaired
  — the worst prior blind spot, the curator's main action, was rewriting link
  targets unaudited); `handedit_service.mark_section`/`clear_section` →
  `wiki.handedit.mark`/`wiki.handedit.clear`; `feedback_service.write_feedback`
  → `wiki.feedback.write`. `AliasService` gained an optional `LedgerWriter`
  (read-only `resolve` callers unaffected); the function-style services gained
  `ledger`/`actor_kind` params. Threaded through `routes/{aliases,handedits,
  feedback}`, `CuratorService` (+ `curate_page_job` passes the project owner as
  actor), and the ingest workflow's alias-register + repair sites.
- Hand-edits now emit a ledger event, which also closes the prior
  "hand-edits write no ledger event → not pushed live" honest-limit.

### Tests
- Unit: `packages/aleph-db/tests/test_chain_verify.py` (3).
- Integration: `tests/e2e/test_ledger_verify.py`, `test_alias_ledger.py`,
  `test_handedit_feedback_ledger.py`. Existing `test_curator_repair.py` (4) stays
  green (the new `CuratorService(session)` ledger/actor params default to None).

### Gates (evidence)
- `ruff check .` → All checks passed. `ruff format --check .` → 0 reformat.
- `pytest -m "not integration"` → 152 passed. Pyright (touched files) → 0 errors.
- `alembic check` → No new upgrade operations (no migration this phase).

## Audit remediation — Phase 2: curator chokepoint + cross_link + robustness (2026-06-30)

Closes F03 (siblings never cross-linked), F04 (curator not enqueued from every
authoring path), F22 (brittle overview identification), F24 (silent skip with no
ModelProfile).

### What shipped
- **F04 — every authoring path knits.** `curate_page_job` is now enqueued from
  bootstrap (overview), notes-promote (route, ad-hoc arq pool, post-commit),
  wiki-ingest (moved OUT of the `if pending is None` mechanical-review guard so
  it fires for every committed revision), and synthesis (already). No in-commit
  hook (would race the job ahead of the caller's commit) — post-commit enqueue at
  each path instead.
- **F03 — cross_link.** `CuratorService._cross_link` + the pure
  `inject_cross_links(body, surfaces)`: wraps the first un-linked prose
  occurrence of an existing sibling title/alias in `[[ ]]` (protects fenced/inline
  code + existing links, longest-surface-wins, word-boundary, idempotent, bounded
  to `_CROSS_LINK_MAX_TARGETS=20`, min surface len 4), then commits a curator
  revision so `WikiLink` rows are written. Runs in `curate()` after repair.
- **F22 — robust overview.** `_find_overview` prefers a stable
  `page_kind == "overview"` marker (bootstrap now sets it; recurate preserves
  `page_kind`); dedup excludes the overview by id. Title-match fallback for legacy
  projects. Renaming a project no longer silently breaks recuration.
- **F24** — the curate job logs `wiki.curate.llm_steps_skipped_no_profile` when a
  project has no ModelProfile (deterministic knit still runs).

### Tests
- Unit: `packages/aleph-wiki/tests/test_cross_link.py` (7).
- Integration: `tests/e2e/test_cross_link_curate.py` (prose→resolved link,
  idempotent). Existing `test_curator_repair.py` (4) stays green.

### Gates
- `ruff check .` clean; `ruff format --check .` clean; `pytest -m "not integration"`
  **159 passed**; pyright (touched) 0 errors; `alembic check` no new ops.

### Note
- Curator logic is validated in-process (the integration tests build the ASGI app
  locally). The enqueue→worker→curate path through the running `aleph-workers`
  container will be re-validated live during the Phase 3 stack rebuild + Playwright.

## Audit remediation — Phase 3: merge-proposal surface + real apply_merge (2026-06-30)

Closes F05 (merge proposals invisible end-to-end) and F23 (apply_merge left
[[source]] text in bodies).

### What shipped
- **F23** — `apply_merge` rewrites `[[Source]] -> [[Target]]` in inbound page prose
  (pure `rewrite_wikilink_target`, label-preserving) by recommitting those pages
  with `origin="curator"`, in addition to the structural `WikiLink` redirect +
  alias + soft-delete. Ledger payload records `links_redirected` + `bodies_rewritten`.
- **F05** — pending `PageMergeProposal`s now render as `ApprovalCard`s on the
  **Briefs** tab (the generic ApprovalCard — no new frontend component); the
  `/cards/actions` router gained `page_merge_proposal` **approve** (→ `apply_merge`)
  and **reject** branches; `wiki_curation_status` reports pending merges so the
  conversational path surfaces them. Also ledgered the synthesis-reject
  `write_feedback` call (a small rule-#4 hole).

### Tests
- Unit: `rewrite_wikilink_target` (3, in `test_cross_link.py`).
- Integration: `test_merge_approve_action.py` (surfaces in briefs + approve via
  `/cards/actions` merges & soft-deletes + reject), `test_merge_body_rewrite.py`.
  `test_curator_repair.py` (4) green.

### Gates
- ruff/format clean; `pytest -m "not integration"` **162 passed**; pyright (touched)
  0 errors; `alembic check` no new ops.

### Note
- The ApprovalCard render + approve/reject is generic frontend that already works;
  the dedicated Playwright `merge-proposal.spec.ts` + live drive ride the stack
  rebuild batched with the other UI phases (6, 9).
