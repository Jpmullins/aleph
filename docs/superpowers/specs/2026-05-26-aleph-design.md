# Aleph — Living Multi-Agent Research Environment

**Status:** Top-level design spec.
**Created:** 2026-05-26.  **Last updated:** 2026-05-27 (wiki-first retrieval + UI rework).
**Supersedes:** `start.md` (preserved in repo for history; substantive changes listed in §3).

> Working principle: every implemented subsystem ships in **final production form** for its declared scope. There is no "v1 with v2 enhancements later." Items not yet implemented are either **explicitly out-of-scope** (§16.1) or **sequenced for a later increment** (§13). Nothing is deferred as a stub.

---

## 1. Vision and Differentiator

Aleph is a research environment where **provenance is structural, not editorial** and **the wiki is the working knowledge base for both the analyst and the assistant**.

The system is built around three persistent layers:

1. **Raw Knowledge Store (RKS)** — ingested source material, normalized text, parser outputs, metadata, hashes, access rules. Upstream of the wiki. Reached on demand, not in primary retrieval.
2. **Compiled Wiki** — a curated, navigable, wikilinked, revisioned project KB. **Primary retrieval surface for both assistant and analyst.** The Karpathy "LLM Wiki" pattern, made multi-agent and audit-grade. (See [reference notes on Karpathy's LLM Wiki and the practical implementation at `obsidian-llm-wiki`](#19-glossary).)
3. **Interactive Analyst Workspace** — three-panel UI where the analyst chats, navigates the wiki, manages artifacts/notes/hypotheses, and dispatches Briefs. The right panel is rendered entirely via A2UI surface components.

Aleph is not "chat with documents" and it is not "RAG over chunks." It is a research operating environment where a curated multi-agent-maintained wiki is the working KB, and raw sources are the upstream material the wiki is compiled from.

---

## 2. First Users and Use Cases

Aleph targets two user profiles with overlapping requirements:

- **OSINT / intelligence analysts** — open-source research with serious source quality, citation rigor, conflict/contradiction detection, structured hypotheses with confidence levels, redaction-aware export.
- **Academic / scientific researchers** — literature reviews, methodology tracking, citation graph maintenance, structured datasets, CSL/BibTeX-compatible export.

Both profiles share: provenance discipline, structured claims, multi-source synthesis, exportable artifacts with lineage.

A first project for either user looks like: *"Given this title and a connector allowlist, build me a wiki that answers these questions with cited claims, flag unsupported claims for review, track open hypotheses, and let me chat with the assistant against the wiki to dig deeper."*

---

## 3. Substantive Changes from `start.md`

Locked design decisions. Each is a deliberate departure from the original draft.

### 3.1 Adopt A2UI as the agent↔UI protocol

We adopt [A2UI](https://a2ui.org) (open Agent-to-UI standard) and ship an **Aleph A2UI Catalog**. A2UI powers **the entire right panel**, not just inline chat cards.

- **Renderer:** `@a2ui/react` npm package. Reference clone at `~/code/A2UI/renderers/react`. The two trajectories to be aware of: A2UI **core** (npm `a2ui`) is at v1.0+; the **React renderer** (`@a2ui/react`) is still on a 0.x line. Both tracked at upstream latest.
- **Transport:** AG-UI / CopilotKit.
- **Agent SDK:** `a2ui` Python package. Reference clone at `~/code/A2UI/agent_sdks/python`. Tracked at upstream latest.
- **Catalog:** Aleph defines five top-level *surface* components (one per right-panel tab) plus the inline component set used in chat and embedded in surfaces. See §11.

**Version policy:** Aleph tracks A2UI's current upstream release across both core and renderer. A CI lane validates the Aleph catalog schema against the current renderer; a breaking upstream change shows up there immediately and is fixed in the increment where it breaks, not preemptively avoided. See §15.6 for the broader upstream-tracking policy.

### 3.2 Split Reviewer into two passes

`start.md` defined one Reviewer doing six unrelated things. Split:

- **MechanicalReviewer** — claim↔citation matching, broken/stale wikilinks and source links, schema validation, hash mismatches, duplicate sources, citation freshness, alias consistency. Auto-runs on every wiki revision. Cheap, deterministic. Reuses AIQ's `citation_verification` (§3.7) for the citation-matching pass.
- **EditorialReviewer** — contradictions, weak source quality, narrative gaps, conflicting claims, factual freshness, coverage gaps in the wiki. Expensive, batched, always human-in-the-loop via `ApprovalCard`.

### 3.3 Wiki is the primary retrieval surface for both assistant and analyst

This is the load-bearing decision. The Karpathy / `obsidian-llm-wiki` pattern, made multi-agent + persistent + auditable.

- **Wiki retrieval = LLM-routed page selection + wikilink-graph traversal.** When the assistant answers a question, it routes over the wiki index (page selector LLM call), reads the selected pages, follows wikilinks as needed, and answers from wiki text with `[[wikilink]]` and `[c12]` citation markers preserved.
- **No RAG over raw chunks in the primary path.** Embeddings are *not* the first-line retrieval. Wiki coverage is what makes the assistant answer correctly.
- **RKS chunk embeddings exist only for intra-source descent.** When a wiki page cites `[[Source:Smith2024]]` and the assistant needs more context, it descends: load that source's normalized text; chunk-retrieve *within that single source*; bring the relevant excerpt back into the response.
- **`--synthesize` is first-class.** When a query isn't covered by the wiki, the assistant proposes a *new* wiki page (or extension to an existing page) synthesizing across cited sources. The proposal goes through the same Reviewer → Approval pipeline. Approved synthesis becomes new wiki content. This is how the KB grows from real questions.
- **Hand-edits are preserved.** Analyst hand-edit to a wiki page records a `HandEditMark` (content-hash snapshot). Subsequent agent compiles skip the hand-edited region unless the analyst clears the mark.
- **Rejection feedback loop.** Analyst rejects a draft page or section with a reason. The reason is fed into the next compilation prompt for that concept. Five rejections without an approval auto-block the concept until the analyst re-enables.
- **Aliases.** `PC` → `Program Counter` extracted at source-ingest time and recorded as `Alias` rows. Used to repair broken wikilinks and to normalize concept names across the project.

Why: RAG over raw chunks is expensive per query, non-deterministic across runs, and (per Karpathy and many practical reports) tends to lose the interconnection that makes a KB useful. A compiled, wikilinked wiki gives cheap deterministic page selection, persistent improvement, and an artifact the analyst can actually navigate and edit. See §19 for the Karpathy + obsidian-llm-wiki references.

### 3.4 New first-class objects

Beyond `start.md`. **Bold** entries are new in this revision.

- **`SourcePage`** — wiki page representing a single source. Lives at `[[Source:<short-id>]]`. Carries provenance metadata, extracted claims, link to the raw `SourceAsset`, generated by the wiki agent at ingest time.
- **`Alias`** — `surface_form → canonical_name` (e.g. `PC → Program Counter`). Extracted at ingest; used to repair wikilinks and normalize concepts.
- **`HandEditMark`** — `(page_id, hash, applied_at, applied_by)` recording an analyst hand-edit so the compiler doesn't clobber it.
- **`RejectionFeedback`** — `(page_id | section_id, reason_text, rejected_at)` fed into next compile.
- **`WikiIndex`** — the assistant's first-line retrieval surface; a denormalized, queryable index of page titles, aliases, summaries, wikilinks, and update timestamps. Rebuilt incrementally on wiki revisions.
- `ModelProfile`, `ModelCall`, `CostLedgerEvent`, `Budget`
- `EvalDataset`, `EvalCase`, `EvalRun`, `EvalResult`, `UserFeedback`
- `Connector`, `ConnectorBinding`, `ConnectorCredential` (with `output_kind ∈ {document, dataset_rows}`)
- `AgentMemory`
- `Citation` — `Claim → DocumentChunk[]` explicit edge

### 3.5 Right panel is 5 tabs, each an A2UI surface

**Wiki | Artifacts | Notes | Hypotheses | Briefs.** No Sources tab (sources live as `SourcePage` wiki pages). No Datasets tab (datasets are domain objects rendered as `ChartCard` / `TableCard` / `GraphCard` / `MapCard` *inside* whatever tab they belong to). No Review queue tab (approvals and findings live in Briefs). See §7.

### 3.6 Left panel is slim

Project switcher + assistant sessions list + bottom icon row (gear / logs / notifications / profile). No in-project navigation tree. See §7.

### 3.7 Adopt NVIDIA AIQ as the Research Agent subsystem

We do **not** rebuild a deep-research agent pipeline. Aleph adopts [NVIDIA AI-Q Blueprint](https://github.com/NVIDIA-AI-Blueprints/aiq) (Apache 2.0) as its research engine, accessed across an HTTP boundary. Aleph runs the AIQ server as a separate worker process. Current AIQ release pinned in deployment manifests; new tags rolled forward per §15.6.

**From AIQ, Aleph uses:**

- **Agent pipeline:** Orchestrator → ShallowResearcher → DeepResearcher (DeepAgents-based) → Clarifier.
- **`data_source_registry`** — AIQ's typed source registry IS Aleph's Connector registry. Each Aleph connector is authored as a `nat`-registered function.
- **Tokenomics** — AIQ's per-phase, per-model token + cost + cache-hit-rate tracking feeds Aleph's `CostLedger` via an adapter.
- **`citation_verification`** — `verify_citations`, `sanitize_report`, `EmptySourceRegistryError`. Used by Aleph's MechanicalReviewer as its citation-matching pass.
- **Custom LangChain middleware** — `SourceRegistryMiddleware`, `ToolNameSanitizationMiddleware`, `ToolResultPruningMiddleware`, `ToolRetryMiddleware`, `EmptyContentFixMiddleware`.
- **Async deep research jobs** — REST `/v1/jobs/async/agents` + SSE.
- **Skills + sandbox** — DeepAgents skills with optional Modal sandbox; off by default in Aleph.

**What changes from AIQ defaults:**

- **LLM transport replaced.** AIQ's `_type: nim` configs are replaced with `_type: openai` pointing at `LITELLM_BASE_URL`. Every AIQ LLM call goes through the Insights LiteLLM Gateway. Model selection per AIQ phase is driven by Aleph's `ModelProfile`.
- Aleph holds the user identity boundary. AIQ receives a service token and correlation ID.
- Aleph owns persistence. AIQ's in-process job state is ephemeral; Aleph services persist durable artifacts via the typed-service-only mutation rule.
- **AIQ's `knowledge_layer` is disabled.** Aleph's wiki/RKS is the canonical KB.

**Output discipline.** AIQ research output is not directly published. It is offered as a synthesis proposal that goes through the wiki agent → reviewer → approval pipeline, becoming wiki content only after approval. This keeps the wiki the single source of truth.

### 3.8 Typed connector output kinds

Connectors declare `output_kind ∈ {document, dataset_rows}`. Document-output connectors feed `Source` → `NormalizedDocument` → `DocumentChunk` (+ a `SourcePage` in the wiki). Dataset-output connectors feed `Dataset` → `DatasetVersion` → `Observation[]` and bind to chart/table/map A2UI cards. Same plugin pattern, different downstream lifecycle.

---

## 4. Architecture

### 4.1 Runtime topology

```
┌────────────────────────────────────────────────────────────────┐
│  Browser                                                       │
│  React + @a2ui/react + CopilotKit (AG-UI transport)            │
│  3-panel shell: left (projects+sessions), center (chat),       │
│  right (A2UI surfaces: Wiki|Artifacts|Notes|Hyp|Briefs)        │
└────────────────────────────────────────────────────────────────┘
                         ▲   ▲
                  HTTP/REST  SSE (AG-UI, Langfuse-tagged)
                         │   │
┌────────────────────────────────────────────────────────────────┐
│  aleph-api  (FastAPI + Pydantic)                               │
│  • Typed services (project, wiki, source, claim, alias, edit,  │
│    card, surface, review, approval, artifact, dataset,         │
│    hypothesis, cost, model, feedback, ledger, trace, eval,     │
│    connector, credential)                                      │
│  • Auth + project-scoping middleware  • A2UI Python SDK        │
│  • Action-ledger writer  • Cost-ledger writer  • Langfuse ctx  │
│  • AIQ client (service token, async jobs, SSE consumer)        │
│  • Wiki retrieval router (page selector LLM call)              │
└────────────────────────────────────────────────────────────────┘
       ▲                            │                          │
       │                            ▼                          ▼
┌──────┴──────────┐       ┌──────────────────────────┐   ┌──────────────────┐
│  aleph-workers  │◄──────│  redis (broker + pubsub) │   │  aiq-server      │
│  • Normalization│       └──────────────────────────┘   │  (NeMo Agent     │
│  • Chunk + embed│                                      │   Toolkit)       │
│    (intra-src)  │       ┌──────────────────────────┐   │  • Orchestrator  │
│  • Wiki agent   │──────►│  Langfuse (traces+evals) │◄──│  • Shallow       │
│    (ingest+     │       └──────────────────────────┘   │  • Deep (DA)     │
│     compile+    │                                      │  • Clarifier     │
│     index)      │                                      │  • data_source_  │
│  • Reviewers    │                                      │    registry      │
│  • Builder      │                                      │  • tokenomics    │
│  • Playwright   │                                      │  • citation_ver. │
│    render       │                                      │  • Connectors as │
│  • Connectors   │                                      │    nat plugins   │
└──────────────────┘                                     └──────────────────┘
       │                                                          │
       ▼                                                          │
┌──────────────────────────────────────────────────────┐          │
│  Postgres + pgvector │ MinIO/S3       │ Qdrant      │          │
│  • domain rows       │ • raw assets   │ (optional)  │          │
│  • wiki pages +      │ • normalized   │             │          │
│    revisions + idx   │   md cache     │             │          │
│  • pgvector chunks   │ • artifacts    │             │          │
│    (intra-source)    │ • renders      │             │          │
│  • action ledger     │                │             │          │
│    (hash-chained)    │                │             │          │
│  • cost ledger       │                │             │          │
└──────────────────────────────────────────────────────┘          │
       ▲                                                          │
       │ all AIQ tool calls (connectors) re-enter via aleph-api   │
       └──── for creds + scope + Source/Dataset writes  ◄─────────┘
```

### 4.2 Retrieval flow (the load-bearing diagram)

```
  User asks → Assistant
       │
       ▼
  ┌────────────────────────────────────────────────┐
  │ 1. WikiIndex page-selector LLM                 │
  │    Input: query, project_id, wiki index        │
  │    Output: top-K wiki page IDs (with reasons)  │
  └────────────────────────────────────────────────┘
       │
       ▼
  ┌────────────────────────────────────────────────┐
  │ 2. Load selected pages + their direct wikilinks│
  │    (1-hop graph expansion, bounded)            │
  └────────────────────────────────────────────────┘
       │
       ▼
  ┌────────────────────────────────────────────────┐
  │ 3. Answer composer LLM                         │
  │    Input: query + page bodies + linked pages   │
  │    Output: answer with [[wikilinks]] and       │
  │            [c12] citation markers preserved    │
  └────────────────────────────────────────────────┘
       │
       ▼
  ┌────────────────────────────────────────────────┐
  │ 4. Coverage check                              │
  │    If composer flags missing info AND it cites │
  │    a SourcePage [[Source:X]] → descend:        │
  │       4a. Load Source X's normalized text      │
  │       4b. Chunk-retrieve within X (pgvector +  │
  │           FTS), bounded to that single source  │
  │       4c. Re-run composer with descent context │
  │    Else if composer flags missing info AND no  │
  │    source covers it → propose --synthesize:    │
  │       4d. Trigger AIQ research (if connectors  │
  │           allowed) → new SourcePage(s) → wiki  │
  │           agent compiles → reviewer → approval │
  └────────────────────────────────────────────────┘
       │
       ▼
   Answer to user, with inline [[Wikilinks]] and [c12]
```

Embeddings appear only at step 4b (intra-source descent). The primary path (1→3) is wiki-page-selection + wikilink-graph traversal.

### 4.3 Process boundaries

- **`aleph-api`** — synchronous request/response and SSE/AG-UI streams. Holds user-identity boundary. Hosts the WikiIndex page-selector. Never blocks on long agent runs.
- **`aleph-workers`** — Wiki agent (ingest/compile/index), reviewers, builder, normalization, intra-source chunking + embedding, Playwright render.
- **`aiq-server`** — AIQ subsystem (NAT runtime). Runs the research pipeline only when the assistant needs to grow the wiki (`--synthesize` flow). Receives a service token + correlation ID.
- **AIQ ↔ tools.** AIQ's connector plugins call back into `aleph-api` over an internal RPC for credential resolution, project scoping, and to persist resulting `Source` rows. AIQ does not write directly to Postgres or S3.

### 4.4 Service layer rules

1. Agents (Aleph or AIQ) never write to Postgres directly. They call typed `aleph-api` service methods over HTTP.
2. Every state-mutating service method writes one `ActionLedgerEvent` in the same transaction as the mutation.
3. Every service method that triggers an LLM, embedder, connector, or AIQ job writes `CostLedgerEvent` rows and is wrapped in a Langfuse span.
4. Every service method runs under an authenticated `Principal` and resolves a `Project` scope.
5. Every persisted row carries: `id`, `project_id`, `created_at`, `updated_at`, `created_by`, `access_scope`, `trace_id?`, `ledger_event_id?`.

### 4.5 Stack

Specific versions live in `package.json` and `pyproject.toml`, not here. The spec names which packages we use; manifests pin them. Per §15.6, all moving deps track upstream latest.

- **Frontend:** React, TypeScript, Tailwind, CopilotKit, `@a2ui/react`, `a2ui` (core)
- **Backend:** Python (current stable), FastAPI, Pydantic, SQLAlchemy, Alembic, httpx, uvicorn
- **Workflow (Aleph):** LangGraph for wiki compile, reviewers, builder, bootstrap
- **Workflow (research):** AIQ (NeMo Agent Toolkit). Vendor pin to the current AIQ release tag; track new tags promptly.
- **Agent harness:** LangChain Deep Agents (`deepagents`) — used by AIQ DeepResearcher and Aleph EditorialReviewer
- **Database:** Postgres (current stable) with `pgvector`
- **Object store:** MinIO local, S3 production
- **Keyword search:** Postgres FTS (for wiki text + intra-source chunks)
- **Observability:** Langfuse + OTEL
- **Event stream:** SSE (AG-UI transport)
- **Queue:** Arq + Redis
- **LLM transport:** **Insights LiteLLM Gateway** (OpenAI-compatible proxy). All LLM and embedding calls from `aleph-api`, `aleph-workers`, and `aiq-server` route through the gateway. Provider routing (Anthropic, OpenAI, Google, etc.) is the gateway's concern. See §9 for `ModelProfile` shape and the env-vars `LITELLM_BASE_URL` + `INSIGHTS_LITELLM_API_KEY`.

**Increment 0 verifies actual current versions against the registries (npm, PyPI, GitHub releases) at bootstrap time** and seeds `package.json` / `pyproject.toml` with the verified latest. The implementation log records the snapshot of versions installed. Subsequent renovate-bot PRs roll forward.

---

## 5. Domain Model

### 5.1 Identity & access
- `User`, `Principal`
- `Project`, `ProjectMember` (role: `owner` | `editor` | `viewer`)

### 5.2 Provenance backbone
- `ActionLedgerEvent` — append-only, hash-chained, every mutation
- `AgentRun`, `AgentEvent` — both Aleph and AIQ runs
- `Trace` — Langfuse pointer

### 5.3 Operations
- `ModelProfile`, `ModelCall`
- `CostLedgerEvent` — fed by AIQ tokenomics adapter + native Aleph LLM calls
- `Budget` — soft/hard thresholds

### 5.4 Raw Knowledge Store (RKS)
- `Source`, `SourceVersion`, `SourceAsset`
- `Connector`, `ConnectorBinding`, `ConnectorCredential` (encrypted)
- `NormalizedDocument`
- `DocumentChunk` (pgvector + FTS) — **used for intra-source descent only**
- `RetrievalIndexRecord` (per-source intra-source index pointer)

### 5.5 Wiki — THE PRIMARY KB
- `WikiPage`, `WikiRevision` (immutable)
- `WikiSection` (sub-page granularity for hand-edit + rejection feedback)
- `WikiLink` — `[[wikilink]]` between pages
- `WikiClaim` — atomic factual assertion with graded confidence
- `Citation` — `WikiClaim → DocumentChunk[]` evidence link, plus `WikiClaim → SourcePage`
- `SourcePage` — wiki page representing one `Source`; the bridge between wiki and RKS
- `Alias` — `surface_form → canonical_name`
- `HandEditMark` — protects analyst edits from compiler clobbering
- `RejectionFeedback` — fed into next compile prompt
- `WikiIndex` — denormalized assistant-retrieval index (titles, summaries, aliases, wikilinks, updated_at)

### 5.6 Review & approval
- `ReviewRun` (`kind ∈ {mechanical, editorial}`)
- `ReviewFinding` (severity, evidence_refs)
- `ApprovalRequest`, `ApprovalDecision`

### 5.7 Assistant & memory
- `AssistantThread`, `AssistantMessage`, `AssistantSession` (UI-grouped threads)
- `AgentMemory` — per-project, per-agent structured scratchpad

### 5.8 Analyst-authored surfaces
- `Hypothesis`, `HypothesisVersion`, `HypothesisEvidence` (→ wiki claims + chunks)
- `Note`, `NoteSection` (analyst notebook; free-form, with `[[wikilink]]` affordances)

### 5.9 Interactive surface (A2UI)
- `InteractiveCard`, `InteractiveCardVersion`
- `CardAction` (ledgered)
- `RenderedAsset` (PNG/SVG/PDF snapshot)

### 5.10 Datasets — DOMAIN OBJECTS, NOT A TAB
- `Dataset`, `DatasetVersion` (immutable snapshot)
- `Observation` (row)
- A `DatasetVersion` is bound by `ChartCard`/`TableCard`/`MapCard`/`GraphCard` instances *inside* a tab (typically Wiki, Notes, or Briefs).

### 5.11 Artifacts — OUTPUT
- `Artifact`, `ArtifactVersion`
- `RenderedAsset` (lifted from §5.9 for artifact-level renders)

### 5.12 Quality
- `EvalDataset`, `EvalCase`, `EvalRun`, `EvalResult`
- `UserFeedback`

---

## 6. Agent System

| Agent | Where it runs | Owns | Critical contract |
|---|---|---|---|
| **Bootstrap** | aleph-workers (LangGraph) | Project init state machine | Idempotent; resumable; ledger event per phase transition |
| **Research (= AIQ pipeline)** | aiq-server | Orchestrator + Shallow + Deep + Clarifier | Triggered when wiki coverage gap is detected; outputs offered as wiki synthesis proposal; never publishes directly |
| **Wiki** | aleph-workers (LangGraph DAG) | Wiki ingest + compile + index + alias extraction; SourcePage creation; coverage maintenance | Only writes via `wiki_service.commit_revision()`; honors `HandEditMark` and `RejectionFeedback` |
| **MechanicalReviewer** | aleph-workers (LangGraph) | Citation matching (via AIQ `citation_verification`), broken/stale wikilinks + source links, schema, hash, dupe, freshness, alias consistency | Produces `ReviewFinding`; never edits content |
| **EditorialReviewer** | aleph-workers (Deep Agents) | Contradictions, weak sources, narrative gaps, coverage gaps in wiki | Proposes patches gated through `ApprovalRequest` |
| **Assistant** | aleph-api + aleph-workers (LangGraph + A2UI SDK) | User chat; wiki page-selection retrieval; intra-source descent; card and surface proposals; `--synthesize` requests | Stateless across turns except via `AgentMemory`; never mutates without service call |
| **Builder** | aleph-workers (LangGraph) | Artifact export | Renders from immutable snapshots only |

### 6.1 Orchestration

Bootstrap → Research (AIQ, seed corpus from connector allowlist) → Wiki agent ingests + compiles initial pages + SourcePages → MechanicalReviewer (auto on every revision) → EditorialReviewer (scheduled + threshold). Assistant is available throughout; during answer composition it can trigger any of the above.

### 6.2 Wiki agent expanded responsibilities

Because the wiki is the primary retrieval surface, the wiki agent's job is larger than `start.md` implied:

- **Coverage.** Every salient claim from every ingested source must land in a topic page or a `SourcePage`. Untracked claims = unanswerable queries.
- **Alias normalization.** Concept names are canonicalized; surface forms recorded as `Alias` rows.
- **Wikilink generation.** Pages link to other pages via `[[wikilinks]]`; the assistant's graph traversal depends on this density.
- **SourcePage maintenance.** Each `Source` gets a wiki page with: structured metadata, extracted claims, link to `SourceAsset`, link to chunks for descent.
- **Index maintenance.** `WikiIndex` is rebuilt incrementally on every revision.
- **Hand-edit respect.** `HandEditMark` regions are not regenerated.
- **Rejection feedback ingestion.** Past rejection reasons inform the next compile prompt for the affected concept.
- **Synthesis proposals.** When the assistant's coverage check fails, the wiki agent receives a synthesis request, drafts a new page (or extension), and routes it through the reviewer/approval pipeline.

### 6.3 Hard agent rules

- **Agent → service is the only path to state.** Agent proposes; service validates, revisions, ledgers, traces, emits SSE.
- **No tool returns text the agent re-issues as state-changing input.** Tools return typed `Proposal` objects.
- **All agent runs traced.** No untraced LLM call.
- **All state-changing operations ledgered.** No untraced ledger gap.
- **AIQ output is not wiki content.** It is a synthesis proposal; the wiki agent + reviewer + approval gate it.

---

## 7. UI/UX

### 7.1 Three-panel shell

```
┌──────────────────┬─────────────────────────────┬──────────────────────┐
│  LEFT PANEL      │  CENTER PANEL               │  RIGHT PANEL         │
│                  │                             │                      │
│  Project switch  │  Assistant chat             │  Tabs:               │
│  ──────────────  │  • messages                 │   ┌──────────────┐   │
│  Sessions in     │  • inline A2UI cards        │   │ Wiki  Art   │   │
│  current project │  • [[wikilinks]] + [c12]    │   │ Notes Hyp   │   │
│  • Session #142  │  • cost/trace badge         │   │ Briefs(3)   │   │
│  • Session #141  │                             │   └──────────────┘   │
│  • Session #140  │  Activity card              │                      │
│  • ...           │  • current plan             │  Active A2UI         │
│                  │  • running agents           │  surface for the     │
│                  │  • blocked / approval       │  selected tab        │
│                  │  • cost so far              │                      │
│  ──────────────  │                             │                      │
│  ⚙ ◐ 🔔 ●        │                             │                      │
└──────────────────┴─────────────────────────────┴──────────────────────┘
   gear logs noti profile
```

### 7.2 Left panel — slim

- **Top:** project switcher → list of assistant sessions in the current project. Click a session → loads its thread in the center panel.
- **Bottom (icon row):** gear (settings), logs, notifications, profile.
- No in-project navigation tree. The right panel handles all in-project content.

This is intentionally the Claude/ChatGPT sidebar pattern. The left panel is for cross-conversation navigation.

### 7.3 Center panel — assistant chat + activity

- Chat messages (user + assistant)
- Inline A2UI cards proposed by the assistant (e.g. `ApprovalCard`, `ChartCard`, small `TableCard`)
- Citations: `[[Wikilink]]` markers hover-preview the wiki page; `[c12]` markers hover-preview the source chunk
- Activity card pinned to bottom showing running agents, blocked ops, cost so far
- Input box

### 7.4 Right panel — 5 tabs, each an A2UI surface

| Tab | Surface component | What it shows | Notes |
|---|---|---|---|
| **Wiki** | `WikiSurface` | The wiki. Page view, graph view, search, wikilink navigation, `SourcePage` view. Inline charts/tables/maps render here where they belong on a page. | The primary KB navigation surface. |
| **Artifacts** | `ArtifactsSurface` | Generated outputs — reports, decks, source packs, exported PDFs. Browser + viewer + export. | Built by Builder agent. |
| **Notes** | `NotesSurface` | Analyst-authored notebook. Free-form sections with `[[wikilink]]` affordances. Charts/tables/sketches embed here. | Analyst's private working journal. |
| **Hypotheses** | `HypothesesSurface` | Structured hypotheses with evidence + confidence + versions. Interactive board. | Analyst-authored structured questions. |
| **Briefs** | `BriefsSurface` | The action pile. Approval requests, review findings, assistant-proposed actions, one-off interactive cards (charts/tables/forms) waiting to be filed elsewhere. Badge count = unhandled items. | Where you handle pending work. |

**A2UI generates the rendering for each surface.** Wiki page rendering, artifact browsing, note editing, hypothesis cards, and Briefs items are all declarative A2UI component compositions. This means the assistant (and the app itself) can compose richer experiences per project, per task, per page — not just plain Markdown rendering.

**Where Sources live.** Each `Source` has a `SourcePage` in the wiki at `[[Source:<short-id>]]`. To browse sources, open the Wiki tab and filter by source-pages (or click any `[c12]` marker to land on its source page). There is no separate Sources tab.

**Where Datasets live.** `Dataset` and `DatasetVersion` are domain objects. They render as `ChartCard` / `TableCard` / `MapCard` / `GraphCard` A2UI cards *inside* the tab where they belong:
- A benchmark visualization that belongs to a topic → on the Wiki page for that topic.
- A working sketch the analyst is iterating on → in Notes.
- A finding's evidence → in a Briefs card.

**Where Reviews live.** All approvals and findings land in Briefs. `ApprovalCard` and `FindingCard` are the canonical card types.

### 7.5 UX rules

1. **Cards over chat for structured output.** Structured data is always an A2UI card or surface section. Chat carries prose and pointers.
2. **Every wikilink and citation is clickable.** `[[Wikilink]]` → Wiki tab navigates to that page. `[c12]` → opens the source chunk preview.
3. **Activity card is honest.** Real plan and state. No fake progress.
4. **Approvals never auto-dismiss.** Briefs tab is a persistent destination with badge count.
5. **Cost is always visible.** Top of UI shows running cost vs budget.
6. **Hand-edits are sticky.** Editing a wiki page marks it; the next compile shows a banner offering to merge agent updates or keep the hand-edit.
7. **Rejection feedback is solicited at reject time.** When the analyst rejects a proposed page or section, the modal asks for a one-line reason before letting them confirm.

### 7.6 Critical screens

- **Project creation wizard** — title, description, connector allowlist, budget cap, model profile choice.
- **Bootstrap screen** — live phase progress (research → ingest → wiki compile → review), real artifact previews as they emerge.
- **Project workspace** — the 3-panel above.
- **Settings (left-panel gear)** — connector credentials, model profile, budget, project members.
- **Logs (left-panel logs icon)** — Action Ledger viewer with per-row trace links.

---

## 8. Provenance, Audit, Observability

### 8.1 Action Ledger (Postgres, append-only)

- One row per state-changing operation.
- Columns: `id`, `project_id`, `actor_id`, `actor_kind` (`user` | `aleph_agent` | `aiq_agent`), `action_kind`, `target_id`, `target_kind`, `payload_jsonb`, `trace_id`, `timestamp`, `prev_event_id`.
- Hash-chained for tamper evidence.
- Immutable. No deletes. Compaction by archival.

### 8.2 Cost Ledger (Postgres)

- One row per `ModelCall`.
- Columns: `id`, `project_id`, `agent_run_id`, `actor_kind`, `model`, `phase`, `input_tokens`, `cached_tokens`, `completion_tokens`, `cost_usd`, `cache_savings_usd`, `purpose`, `trace_id`, `timestamp`.
- Fed by AIQ tokenomics adapter + native Aleph LLM calls.
- Roll-up views per project, agent run, phase.

### 8.3 Langfuse + OTEL

- Every agent run = trace.
- Every LLM/tool/retrieval call = span.
- AIQ writes OTEL spans; collector forwards to Langfuse.
- Trace ID is written into ledger events and domain rows.

---

## 9. Cost and Model Routing

### 9.1 LiteLLM transport + ModelProfile

All LLM and embedding traffic from Aleph (and from AIQ) routes through a single OpenAI-compatible proxy: the **Insights LiteLLM Gateway**. Provider selection (Anthropic, OpenAI, Google, NIM, etc.) is the gateway's concern. Aleph and AIQ send model names; the gateway routes.

**Deployment env vars** (set in compose / k8s secrets, never in source):
- `LITELLM_BASE_URL` (e.g. `https://gateway.insights.arlis.umd.edu`)
- `INSIGHTS_LITELLM_API_KEY` (the gateway bearer token)

**`ModelProfile` shape** stays per-project; only the binding's `provider` is fixed to `"litellm"`:

```python
class ModelProfile:
    id: UUID
    project_id: UUID
    capabilities: dict[Capability, ModelBinding]
    # Capability ∈ {synthesis, extraction, page_selection, classification,
    #               embedding, rerank, vision, code, judge}

class ModelBinding:
    model: str             # gateway-side model name (e.g. "claude-opus-4-7")
    provider: str          # always "litellm" in this deployment
    fallback: ModelBinding | None
    max_input_tokens: int
    cost_per_input_token_usd: Decimal
    cost_per_output_token_usd: Decimal
    cache_discount_pct: Decimal
```

**Two named default profiles**, gateway model list confirmed 2026-05-27. The profile installed for a new project is selected by `ALEPH_DEFAULT_MODEL_PROFILE` env (`dev` for local compose, `production` for deployed environments). `owner` can override per project.

**`aleph-dev` profile** — cheap *within the same family* as production. Capacity differs; character does not.

| Capability | Model | Why |
|---|---|---|
| `synthesis` | `claude-sonnet-4-6` | Wiki page compile, deep research synthesis. ~5× cheaper than Opus; still produces coherent multi-paragraph cited wiki pages. |
| `judge` | `claude-sonnet-4-6` | Consistent with dev synthesis. |
| `page_selection` | `claude-haiku-4-5` | Wiki retrieval router. Fast and cheap. |
| `extraction` | `claude-haiku-4-5` | Claim/entity/alias extraction. Schema-constrained, Haiku handles it. |
| `vision` | `claude-haiku-4-5` | Cheapest multimodal in family. |
| `classification` | `claude-haiku-4-5` | Same as production. |
| `embedding` | `cohere-embed-english-v3` | English-only is fine for dev corpora; cheaper than v4. |

**Why same-family dev/prod, not open-weight dev:** behavioral drift between Anthropic and gpt-oss/Gemma is real — instruction following, tool-call shape, citation behavior, refusal patterns. Running open-weight in dev means chasing ghosts when prod behaves differently. Keep the family fixed; only flex capacity. Open-weight models are available on the gateway and exposed via project-level `ModelProfile` overrides for operators who want to validate provider-agnosticism in a dedicated CI lane.

**`aleph-production` profile** — premium quality across the board, swap in at deploy.

| Capability | Model | Why |
|---|---|---|
| `synthesis` | `claude-opus-4-7` | Wiki page compile, deep research, Builder reports. Highest-quality reasoning. |
| `judge` | `claude-opus-4-7` | Eval scoring, MechanicalReviewer adjudication. Behavioral consistency with synthesis. |
| `page_selection` | `claude-sonnet-4-6` | Wiki retrieval router. Latency-sensitive; mid-tier reasoning is sufficient. |
| `extraction` | `claude-sonnet-4-6` | Claim/entity/alias extraction. |
| `vision` | `claude-sonnet-4-6` | Claude 4.x family is multimodal; Sonnet is the value sweet spot for PDF figure interpretation. |
| `classification` | `claude-haiku-4-5` | Intent classification, freshness checks. |
| `embedding` | `cohere-embed-v4` | Multilingual + multimodal current-generation embedding for intra-source descent. |

**All gateway-served models** (project-level overrides via `ModelProfile`):
- LLMs: `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`, `gpt-oss-120b`, `gpt-oss-20b`, `gemma-3-27b`, `gemma-3-12b`
- Embeddings: `cohere-embed-v4`, `cohere-embed-english-v3`, `titan-embed-v2`

**Fallback policy.** Default fallback is `None` per binding. Aleph does **not** auto-substitute across model families on rate-limit (Claude → gpt-oss → Gemma) because prompts and behavior differ; an `owner` can configure an explicit fallback chain per capability per project. Fallbacks are ledgered when triggered.

**Promotion path.** Switching a project from `dev` → `production` is a single `ModelProfile` update (ledgered). Eval suite re-runs against both profiles in CI so quality gates are profile-aware (an Aleph-Coverage failure under `dev` blocks dev work; a failure under `production` blocks deployment). See §18.

Capability mappings are recorded in `ModelProfile` rows. Changes are ledgered. Increment 0 seeds both profiles and verifies the gateway's `/v1/models` matches expected names.

**Why LiteLLM in front:**
- Single auth boundary for every LLM call.
- Provider swap without touching Aleph or AIQ.
- Per-tenant rate limits, quotas, and audit live at the gateway.
- AIQ's `_type: openai` config works directly against LiteLLM with `base_url` overridden — no NIM dependency.

### 9.2 Pre-flight cost estimation

Before any operation > $X (configurable, default $1.00), the agent calls `cost_service.estimate(plan)`. If estimated cost would push the project past remaining budget headroom, user confirms.

### 9.3 Budget enforcement

- **Soft cap** (default 80%): banner; operations continue.
- **Hard cap** (100%): new agent operations refuse to start.
- Budget changes are ledgered. Only `owner` can raise.

---

## 10. Connectors

Connectors are typed source-kind plugins, authored as `nat` (NeMo Agent Toolkit) functions registered with AIQ's `data_source_registry`.

```python
class ConnectorBase(Protocol):
    kind: str
    output_kind: Literal["document", "dataset_rows"]
    requires_auth: bool
    metadata_schema: type[BaseModel]

    async def search(query: SearchQuery, project: ProjectScope) -> list[ConnectorResult]: ...
    async def fetch(result: ConnectorResult, project: ProjectScope) -> RawPayload: ...
    async def normalize(payload: RawPayload) -> NormalizedDocument | DatasetRows: ...
```

### 10.1 Roster

| Connector | `kind` | `output_kind` | Auth | Origin |
|---|---|---|---|---|
| **Upload** | `upload` | `document` | none | Aleph-built |
| **Tavily** | `web_search` | `document` | API key | Adopt from AIQ |
| **Exa** | `web_search` | `document` | API key | Adopt from AIQ |
| **Serper / Google Scholar** | `paper_search` | `document` | API key | Adopt from AIQ |
| **arXiv** | `paper_search` | `document` | none | Aleph-built |
| **Semantic Scholar** | `paper_search` | `document` + citation_graph hint | API key (optional) | Aleph-built |
| **OpenAlex** | `paper_search` | `document` + citation_graph hint | none (email tag) | Aleph-built |
| **Lens.org** | `paper_search` | `document` + patents | API key | Aleph-built |
| **RSS** | `feed` | `document` | none | Aleph-built |
| **artificialanalysis.ai** | `structured_api` | `dataset_rows` | API key | Aleph-built |
| **HuggingFace Hub** | `model_repo` | `document` | optional | Aleph-built |

Each connector is implemented complete when its increment lands. No stubs.

### 10.2 Lifecycle

- **`output_kind=document`** → `Source` row → `NormalizedDocument` → `DocumentChunk[]` + `SourcePage` in the wiki.
- **`output_kind=dataset_rows`** → `Dataset` + `DatasetVersion` + `Observation[]`. No `Source` row, no chunks.

### 10.3 Permissions

A project allowlists connectors at creation. AIQ's research workflow refuses disallowed connectors **visibly** (raises, surfaces in the assistant UI).

### 10.4 Credentials

`ConnectorCredential` per-project, encrypted at rest (envelope encryption via cloud KMS or libsodium for dev). Connectors fetch credentials by calling back into `aleph-api` — never from environment variables in the AIQ container.

**Dev-time defaults.** Deployment env may provide development/shared credentials for ungated connectors: `TAVILY_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `ARTIFICIALANALYSIS_API_KEY`, `OPENAI_API_KEY` (for any direct-OpenAI fallback paths and gateway-bypass tooling). These act as the *fallback* `ConnectorCredential` used when a project has not set its own. Per-project credentials always take precedence. `LENS_API_KEY` is pending refresh by the operator and is left blank in the default credential set; the Lens.org connector is registered but disabled by default until the credential is provided.

**LLM-side credentials.** The Insights LiteLLM Gateway bearer (`INSIGHTS_LITELLM_API_KEY`) is a single deployment-level secret used by `aleph-api`, `aleph-workers`, and `aiq-server`. It is not a `ConnectorCredential`.

---

## 11. A2UI Aleph Catalog

Two layers:

### 11.1 Surface components (one per right-panel tab)

| Component | Tab | What it renders |
|---|---|---|
| `WikiSurface` | Wiki | Page view + graph view + search + wikilink navigation + `SourcePage` view + inline card embeds |
| `ArtifactsSurface` | Artifacts | Artifact browser + viewer + export controls |
| `NotesSurface` | Notes | Note editor with `[[wikilink]]` affordances; embeds inline cards |
| `HypothesesSurface` | Hypotheses | Interactive board of `HypothesisCard`s with evidence panels |
| `BriefsSurface` | Briefs | List of action cards (approvals, findings, generated cards) with badge count |

### 11.2 Inline cards (used in chat and embedded in surfaces)

| Component | Purpose | Server backing |
|---|---|---|
| `ClaimCard` | Render a claim with citations + confidence | `WikiClaim` + `Citation[]` |
| `SourceCard` | Source preview + provenance | `Source` / `SourcePage` |
| `ChartCard` | Vega-Lite chart bound to `DatasetVersion` | `DatasetVersion` |
| `TableCard` | Tabular data with sort/filter | `Dataset` |
| `MapCard` | MapLibre GL map | `DatasetVersion` (geo) |
| `GraphCard` | React Flow graph | `DatasetVersion` (nodes+edges) |
| `ApprovalCard` | Approve/reject a patch | `ApprovalRequest` |
| `FindingCard` | Review finding with severity + evidence | `ReviewFinding` |
| `HypothesisCard` | Hypothesis editor | `Hypothesis` |
| `NotebookCellCard` | Markdown cell with link affordances | `NoteSection` |
| `FormCard` | Structured input | none (transient) |
| `DiffCard` | Wiki revision diff | `WikiRevision` pair |

The catalog is versioned. Adding a component is a schema bump + renderer ship.

**Security:** A2UI's catalog model means agents can only request components by name + props. Properties are validated against the JSON Schema at the renderer; invalid surfaces are rejected and logged.

---

## 12. Security and Permissions

### 12.1 Auth
OIDC-compatible IdP. JWT bearer in every request. Default = Auth0 / Cognito / Keycloak.

### 12.2 Authorization
All resources project-scoped. Roles: `owner` / `editor` / `viewer`. No global resources.

### 12.3 Sandboxing
- Aleph workers have no Postgres credentials; they call `aleph-api` with a short-lived agent token scoped to one `AgentRun`.
- AIQ server runs in its own container with a service token; no Postgres or S3 credentials.
- Connector fetches run in egress-restricted worker pods; outbound allowlisted per connector.
- Playwright render workers run with no DB or object-store credentials.
- AIQ's optional Modal sandbox is off by default, opt-in per project.

### 12.4 Audit
Action Ledger is the audit log. Hash-chain verifiable.

---

## 13. Build Increments

Each increment is built to its **final production form** for its declared scope. After each, the coding agent updates `docs/implementation-log.md` with: what was built, files changed, migrations, tests added, trace and ledger behavior added, manual verification, known issues, next entry point.

No timelines. The unit is *increment*, not *week*.

### Increment 0 — Foundations, ledger, cost spine, LiteLLM transport

- Monorepo: `apps/web`, `apps/api`, `apps/workers`; packages `aleph-core`, `aleph-db`, `aleph-security`, `aleph-observability`, `aleph-models`, `aleph-evals`
- Docker Compose: Postgres+pgvector, MinIO, Redis, Langfuse, OTEL collector
- FastAPI shell, React shell, worker shell
- Alembic migrations
- Auth middleware + `Principal` resolution
- Models: `User`, `Project`, `ProjectMember`, `ActionLedgerEvent` (hash-chained), `AgentRun`, `AgentEvent`, `ModelProfile`, `ModelCall`, `CostLedgerEvent`, `Budget`
- **LiteLLM transport client** in `aleph-models`: OpenAI-compatible client pointed at `LITELLM_BASE_URL` with `INSIGHTS_LITELLM_API_KEY`. Single chokepoint for every LLM call. Health check + model enumeration (`GET /v1/models`) at boot.
- **Two seeded `ModelProfile`s — `aleph-dev` and `aleph-production`** (§9.1). Boot selects the per-project default via `ALEPH_DEFAULT_MODEL_PROFILE` env. Model enumeration is checked against expected names; mismatch fails boot loudly.
- Langfuse + OTEL wiring (LiteLLM call → span → ledger)
- CI: lint, typecheck, test, migration check
- One-command local boot
- Docs: `local-development.md`, `repo-structure.md`, `quality-gates.md`, `ledger.md`, `cost.md`, `auth.md`, `litellm-transport.md`

**End state:** create a project, see its empty workspace, ledger captures the create. Cost ledger schema and writer in place even before any wiki call exists. A simple `POST /v1/chat/completions` round-trip through the gateway is verified, traced, and ledgered.

### Increment 1 — RKS + intra-source retrieval + wiki skeleton

The minimum to support wiki-first retrieval starts here.

- Models: `Source`, `SourceVersion`, `SourceAsset`, `NormalizedDocument`, `DocumentChunk`, `RetrievalIndexRecord`
- Upload connector (PDF/MD/TXT/DOCX/HTML/EPUB), running as `nat` function locally (no AIQ server yet)
- Normalization worker (pypandoc + native parsers)
- Chunking + embedding worker — embeddings *only* indexed per-source for intra-source descent
- Models: `WikiPage`, `WikiRevision`, `WikiSection`, `WikiLink`, `WikiClaim`, `Citation`, `SourcePage`, `Alias`, `HandEditMark`, `RejectionFeedback`, `WikiIndex`
- Wiki service with immutable revisions + hand-edit detection
- Wiki agent (LangGraph DAG) — ingest path: for each uploaded source, extract concepts, write `SourcePage`, extract aliases
- WikiIndex builder
- Three-panel UI shell with empty right panel surfaces
- Docs: `rks.md`, `wiki.md`, `wiki-retrieval.md`, `hand-edits.md`, `aliases.md`

**End state:** drop a PDF; system extracts concepts; a `SourcePage` appears in the Wiki tab; aliases populated. No assistant chat yet. The KB skeleton is real.

### Increment 2 — Wiki-first chat + assistant

- `AssistantThread`, `AssistantMessage`, `AssistantSession`
- Wiki retrieval router (page-selector LLM via `ModelProfile.page_selection`)
- Assistant agent (LangGraph) — wiki page-selection + 1-hop wikilink expansion + answer composer
- Intra-source descent path (when assistant flags coverage gap and cites a SourcePage)
- Center-panel chat UI with inline `[[wikilink]]` and `[c12]` citation markers
- Hover-preview for wikilinks and citations
- Cost banner; budget enforcement
- Docs: `assistant-retrieval.md`, `wiki-page-selection.md`

**End state:** **wiki-first chat is real**. Drop sources → wiki gets `SourcePage`s and topic pages → ask questions → assistant retrieves from wiki → cites with `[[wikilink]]` + `[c12]`. RAG-over-chunks does not exist in the primary path. **This is the moment Aleph becomes Aleph.**

### Increment 3 — AIQ integration + full connector roster + `--synthesize`

- Vendor AIQ at current release tag (verified at increment-start, not assumed); AIQ LLM configs rewritten to `_type: openai` with `base_url=LITELLM_BASE_URL` so every AIQ LLM call goes through the Insights gateway
- `aiq-server` in compose with service-token auth
- AIQ tokenomics adapter → CostLedger (tokenomics reads through the same gateway model accounting)
- Connector models: `Connector`, `ConnectorBinding`, `ConnectorCredential` (encrypted at rest; dev-time fallback keys from env per §10.4)
- Author + adopt connectors: Tavily (key in env), Exa, Serper, arXiv, Semantic Scholar (key in env), OpenAlex (email tag), Lens.org (registered, disabled — credential pending refresh per §10.4), RSS, artificialanalysis.ai (key in env; `dataset_rows` path lands here), HuggingFace Hub
- AIQ Orchestrator + ShallowResearcher invokable from assistant for low-cost wiki-extension queries
- AIQ DeepResearcher invokable as `--synthesize` action when wiki coverage gap is detected
- Synthesis proposals route to wiki agent → reviewer → approval
- Project creation wizard now offers connector allowlist + budget + model profile
- Docs: `aiq-integration.md`, `connectors/<each>.md`, `credentials.md`, `synthesize.md`

**End state:** assistant can answer questions, detect wiki gaps, trigger research, propose new wiki pages, route through review. KB grows from real queries.

### Increment 4 — A2UI + Aleph Catalog + Interactive Workspace surfaces

- A2UI Python SDK in agent layer
- A2UI React renderer integration
- **Surface components**: `WikiSurface`, `ArtifactsSurface` (stub view, full Artifacts in Inc 7), `NotesSurface`, `HypothesesSurface` (stub board, full Hypotheses in Inc 5), `BriefsSurface`
- Inline cards: `ClaimCard`, `SourceCard`, `FindingCard`, `ApprovalCard`, `TableCard`, `DiffCard`, `FormCard`, `NotebookCellCard`
- Models: `InteractiveCard`, `InteractiveCardVersion`, `CardAction`
- Wiki tab fully rendered via `WikiSurface` with graph view + page view + search
- Notes tab fully rendered via `NotesSurface`
- Briefs tab renders pending approvals/findings via `BriefsSurface`
- Docs: `a2ui-catalog.md`, `a2ui-surfaces.md`, `interactive-workspace.md`

**End state:** the right panel is real, A2UI-rendered, and the entire UX described in §7 lights up. Wiki navigation, notes, briefs handling all work.

### Increment 5 — Reviewers + approval workflow + Hypotheses

- Models: `ReviewRun`, `ReviewFinding`, `ApprovalRequest`, `ApprovalDecision`, `Hypothesis`, `HypothesisVersion`, `HypothesisEvidence`, `AgentMemory`
- MechanicalReviewer (LangGraph): citation matching (AIQ `citation_verification`), broken/stale wikilinks + source links, schema, hash, dupe, freshness, alias consistency. Runs on every wiki revision.
- EditorialReviewer (Deep Agents): contradictions, weak sources, narrative gaps, coverage gaps. Scheduled + threshold.
- ApprovalCard + DiffCard fully functional; Briefs handles the pile
- Hypotheses surface fully functional with `HypothesisCard`
- Rejection feedback loop wired into wiki agent's next-compile prompts
- Docs: `reviewers.md`, `approval-workflow.md`, `hypotheses.md`, `rejection-feedback.md`

**End state:** wiki revisions get auto-validated; editorial findings flow to Briefs; hypotheses are first-class; rejection feedback loop closes.

### Increment 6 — Datasets + visualization cards

- Models: `Dataset`, `DatasetVersion`, `Observation`
- A2UI cards: `ChartCard` (Vega-Lite), `MapCard` (MapLibre), `GraphCard` (React Flow)
- artificialanalysis.ai connector full implementation (rows → `Observation`s)
- Dataset editing UI (inline value correction, ledgered)
- Immutable `DatasetVersion` snapshots bound to cards
- Cards embeddable in `WikiSurface`, `NotesSurface`, `BriefsSurface`
- Docs: `datasets.md`, `visualization-cards.md`, `data-snapshots.md`

**End state:** charts/maps/graphs bound to immutable dataset snapshots; embeddable anywhere in the right panel.

### Increment 7 — Builder + RenderedAssets + Artifacts

- Models: `RenderedAsset`, `Artifact`, `ArtifactVersion`
- Playwright render worker (sandboxed)
- PNG/SVG/PDF export from cards
- Builder agent: markdown report → PDF / DOCX with source appendix + citations + CSL-aware bibliography
- ArtifactsSurface fully functional with browser + viewer + export
- Docs: `rendered-assets.md`, `builder-agent.md`, `export-formats.md`

**End state:** exportable cited reports; immutable lineage end-to-end.

### Increment 8 — Eval suite + UserFeedback + regression gates

- Models: `EvalDataset`, `EvalCase`, `EvalRun`, `EvalResult`, `UserFeedback`
- Inline feedback affordances (👎 on claim, "mark wrong" on source, "misleading" on chart)
- Fixture project corpus
- E2E eval suite: wiki retrieval accuracy, wiki coverage, citation correctness, reviewer recall, A2UI validation, permission leakage
- Cost regression detection
- AIQ benchmark adapter (FreshQA, DeepResearch Bench) wired into eval runner
- CI gates: regression failure blocks merge; permission leakage blocks merge
- Docs: `evals.md`, `regression-suite.md`, `permission-tests.md`, `cost-regression.md`

**End state:** full assurance suite runs in one command; analyst feedback feeds eval datasets; regressions block merges.

---

## 14. Definition of Done (per increment)

An increment is not complete unless:

- all code is real, no placeholders, no unreachable stubs
- all reachable paths are implemented
- all DB migrations included and applied
- all critical paths tested
- permissions enforced at the service layer
- Langfuse traces + OTEL spans exist for every LLM/tool/retrieval call
- ledger events exist for every mutation
- errors are explicit (no silent failures)
- UI shows real state (no fake progress)
- subsystem docs updated
- runbook `docs/operations/runbook.md` updated
- `docs/implementation-log.md` updated with: what was built, files changed, migrations, tests added, trace + ledger behavior added, manual verification, known issues, next entry point
- cost ledger covers all model/tool/connector calls in scope for this increment

---

## 15. Engineering Rules

Non-negotiable.

### 15.1 No placeholder code in production paths
Forbidden: `TODO: implement later`, `pass` in reachable code, `NotImplementedError` in reachable code, mock ingestion in production, fake progress, stubbed agents, hardcoded example projects, temporary schema, in-memory state for persisted workflows. Allowed only in tests.

### 15.2 No "later hardening"
Security, tracing, provenance, permissions, cost tracking are core behavior, not cleanup. Every new feature ships with: DB model, API contract, service impl, tests, trace instrumentation, ledger events, docs, failure behavior, permission behavior.

### 15.3 No hidden agent behavior
Agents propose; services validate, revise, ledger, trace, emit. No agent (Aleph or AIQ) mutates state directly.

### 15.4 No arbitrary LLM-generated executable code
Cards and surfaces are declarative A2UI. No agent-generated JavaScript runs in the browser. No agent-generated SQL runs against Postgres. AIQ Modal sandbox opt-in only.

### 15.5 Wiki is the KB
Don't bypass it. Don't add a "secret RAG" path. Coverage gaps trigger synthesis, not embedding-fallback.

### 15.6 Track upstream, don't sit on old pins
For moving external dependencies — A2UI, AIQ, CopilotKit, LangGraph, Deep Agents, NAT, the renderers, agent SDKs — Aleph tracks the current upstream release rather than freezing on an old pin. Concretely:

- npm and Python deps use caret/compatible ranges by default; renovate-bot (or equivalent) opens upgrade PRs as upstream releases land.
- CI runs the full eval + schema-validation suite against the upgrade PR. Green → merge. Red → fix in the same PR, not in a "v2 later."
- Major-version bumps with breaking changes are absorbed in the increment they land, not deferred. The cost of a fix-it-now upgrade is reliably lower than the cost of a stale fork that diverges over months.
- Strict pins are allowed only for: (a) infrastructure that absolutely must be reproducible byte-for-byte (the LiteLLM gateway URL, the AIQ tag once we vendor as submodule), (b) packages whose upstream is unreliable enough to warrant snapshotting.
- Things move fast. Sitting on a six-month-old version of an evolving protocol like A2UI or an evolving agent framework like AIQ is more expensive than the upgrade churn.

---

## 16. Scope Boundaries

### 16.1 Explicitly out of scope (will not be built)

- **Multi-project shared knowledge.** Sources/wiki/datasets shared across projects. Aleph is project-scoped end-to-end.
- **Real-time co-editing.** Multiple `ProjectMember`s editing simultaneously with CRDT merging.
- **Cross-project search.** Requires multi-project knowledge first.
- **Public/published wikis.** Aleph wikis are private to project members.
- **Generative UI from LLM-emitted code.** A2UI declarative catalog only. No `eval()` of agent output.
- **Custom model fine-tuning.** Aleph consumes model APIs.

### 16.2 Sequenced for a later increment (in scope; will land complete)

- **CSL-JSON bibliography output** — Increment 7 (Builder).
- **Citation graph traversal across Semantic Scholar / OpenAlex** — follow-on connector increment after Increment 3 if analyst flow demands.
- **Note cell types beyond markdown** (code cells, query cells) — post-Increment 5 when patterns emerge.
- **Project import/export** — post-Increment 8.
- **Connector-as-MCP-server adapter** — follow-on connector increment after Increment 3.

### 16.3 Decisions still open (resolve before affected increment)

- **AIQ vendoring strategy.** Submodule vs pinned PyPI dep vs fork. Decide before Increment 3. Leaning submodule for ease of patching while tracking upstream tags.
- **WikiIndex storage shape.** Single denormalized table vs materialized view vs Elasticsearch. Decide before Increment 1. Leaning Postgres-only with denormalized table.
- **Lens.org credential.** Operator action pending; connector registered but disabled at deploy.

*(Resolved 2026-05-27: default `ModelProfile` mapping pinned against the verified gateway model list — §9.1. A2UI version policy resolved as "track upstream latest" — §3.1 + §15.6.)*

---

## 17. Competitive Landscape

| Product | What it does | Why Aleph differs |
|---|---|---|
| **Perplexity Pages** | Topic-scoped LLM-curated wiki pages with web citations | Read-only, no ingestion lifecycle, no project scoping, no analyst workflow, no audit |
| **NotebookLM** | Upload sources, chat with them | No persistent compiled wiki, no claim-level provenance, no review queue, no export with lineage |
| **Elicit** | Literature review with paper search, structured tables | Strong on academic search; weak on synthesis-as-wiki and ledgered approval |
| **Glean / Coda AI** | Enterprise knowledge search + chat | Org-wide search surface, not project-scoped research workspaces |
| **Open Deep Research / GPT-Researcher** | Single-shot deep research reports | No persistent state, no wiki, no review |
| **AIQ (NVIDIA)** | Research agent pipeline with eval harness | We use it. AIQ is the engine; Aleph is the cabin. |
| **Notion AI** | Generic doc + AI | No structured claims, no source ingestion lifecycle, no audit |
| **obsidian-llm-wiki** | The Karpathy LLM Wiki pattern in Obsidian | Single-user, local, no multi-agent reviewer, no A2UI, no cost ledger. We adopt its patterns and lift to a multi-agent audit-grade product. |

Aleph's distinguishing combination: **wiki-first retrieval + multi-agent compile/review + project-scoped + persistent + cited + revisioned + interactive A2UI surfaces + auditable + cost-tracked**.

---

## 18. Eval Strategy

### 18.1 Public benchmarks (inherited via AIQ)
- **FreshQA** — currency of cited answers.
- **DeepResearch Bench + Bench II** — long-form citation-backed research quality.

### 18.2 Aleph-specific eval datasets

- **Aleph-Coverage** — given a fixture source corpus + ground-truth questions, measures whether the wiki contains the answers. Critical because wiki-first retrieval depends on coverage.
- **Aleph-Page-Selection** — query → ground-truth wiki page set. Measures page-selector LLM precision/recall.
- **Aleph-Citations** — wiki pages with deliberately broken citations. Measures MechanicalReviewer recall.
- **Aleph-Conflicts** — source sets with known contradictions. Measures EditorialReviewer recall.
- **Aleph-Permissions** — multi-project workspaces; measures permission leakage (must be zero).
- **Aleph-Hypotheses** — partial evidence sets; measures hypothesis update appropriateness.
- **Aleph-Descent** — questions where the wiki cites a source but doesn't have the detail; measures intra-source descent success rate.

### 18.3 Cost regression
Every CI run captures per-phase token + cost roll-ups against the fixture corpus. Drift > 15% from baseline alarms.

### 18.4 UserFeedback as ground truth
`UserFeedback` events become eval cases. "Claim marked wrong" with reviewer note = high-signal regression test.

### 18.5 Gates
- Permission leakage → blocks merge (profile-independent).
- Wiki coverage < threshold under `aleph-dev` → blocks dev work.
- Wiki coverage < threshold under `aleph-production` → blocks deployment.
- Citation broken rate > threshold (either profile) → blocks merge.
- Cost drift > 15% (per profile, baseline tracked separately) → alarm.

The eval suite runs against both `ModelProfile`s in CI. The two profiles have different baselines for cost and (within tolerance) quality; gates check against the matching baseline. This catches a regression in `dev` (cheap path quality slipped enough to invalidate eval signal) as well as a regression in `production` (premium path got worse).

---

## 19. Glossary

- **A2UI** — Agent-to-User Interface protocol, a2ui.org. Powers the entire right panel via Surface components, plus inline cards in chat.
- **Action Ledger** — append-only hash-chained Postgres table of every state-changing operation.
- **AgentRun** — one execution of a LangGraph or AIQ workflow.
- **AgentMemory** — per-project, per-agent structured scratchpad.
- **AIQ** — NVIDIA AI-Q Blueprint (Apache 2.0). Aleph's research subsystem.
- **Alias** — `surface_form → canonical_name` mapping. Extracted at ingest. Used to repair wikilinks.
- **Bootstrap** — orchestration workflow initializing a project.
- **Briefs** — fifth right-panel tab. Holds approval requests, review findings, assistant-proposed actions, one-off interactive cards waiting to be filed.
- **Catalog** — set of A2UI Surface and inline components a renderer supports. Aleph ships its own.
- **Citation** — explicit `Claim → DocumentChunk[]` or `Claim → SourcePage` evidence row.
- **Connector** — typed source-kind plugin (`upload`, `web_search`, `paper_search`, `feed`, `structured_api`, `model_repo`).
- **CostLedger** — append-only Postgres table of every LLM/tool/connector cost.
- **DeepAgents** — LangChain's harness. Used by AIQ DeepResearcher and Aleph EditorialReviewer.
- **DocumentChunk** — embedded retrieval unit. Used for **intra-source descent only**.
- **HandEditMark** — protects an analyst-edited wiki region from compiler clobbering.
- **Karpathy LLM Wiki** — Andrej Karpathy's pattern (referenced in obsidian-llm-wiki's README): LLM synthesizes and cross-references; raw notes are source material; wiki persists and compounds. Aleph adapts this to a multi-agent audit-grade product.
- **LiteLLM Gateway** — OpenAI-compatible proxy fronting multiple LLM providers. Aleph's deployment uses the Insights gateway as the single LLM transport for `aleph-api`, `aleph-workers`, and `aiq-server`. Configured via `LITELLM_BASE_URL` + `INSIGHTS_LITELLM_API_KEY` env.
- **MechanicalReviewer / EditorialReviewer** — split reviewer agents.
- **ModelProfile** — per-project capability → model mapping.
- **NAT** — NeMo Agent Toolkit. AIQ's workflow runtime.
- **obsidian-llm-wiki** — practical reference implementation of the Karpathy LLM Wiki pattern (clone at `~/code/obsidian-llm-wiki-local`). Aleph adopts its retrieval, hand-edit, rejection-feedback, alias, source-page patterns.
- **Output kind** — typed connector lifecycle (`document` vs `dataset_rows`).
- **Page selection** — the LLM-routed wiki retrieval primitive: query → top-K wiki page IDs.
- **Principal** — authenticated subject.
- **Project** — top-level scoping unit.
- **RejectionFeedback** — rejection reason fed into the next compile prompt for the same concept.
- **RKS** — Raw Knowledge Store. Sources, assets, normalized text, chunks. Upstream of the wiki. Reached on demand.
- **SourcePage** — wiki page representing one `Source`, at `[[Source:<short-id>]]`. The bridge between wiki and RKS.
- **Surface component** — A2UI top-level component for a right-panel tab (`WikiSurface`, `ArtifactsSurface`, `NotesSurface`, `HypothesesSurface`, `BriefsSurface`).
- **Synthesize** — first-class action: assistant proposes a new wiki page when coverage is missing, routed through reviewer + approval.
- **Tokenomics** — AIQ's per-phase per-model cost analysis. Feeds Aleph CostLedger.
- **Wiki** — the primary KB. Compiled, curated, wikilinked, revisioned, multi-agent-maintained, hand-editable.
- **WikiIndex** — denormalized index used by the assistant's page-selector retrieval.

---

## 20. Appendix: Diagrams

Three diagrams accompany this spec (Excalidraw, in this directory):

- `2026-05-26-aleph-architecture.excalidraw` — runtime topology + AIQ subsystem + retrieval-flow inset
- `2026-05-26-aleph-domain.excalidraw` — domain object graph (wiki at the center, RKS upstream, datasets/artifacts/analyst-authored peripheral)
- `2026-05-26-aleph-ui.excalidraw` — three-panel project workspace (slim left panel + 5-tab right panel)
