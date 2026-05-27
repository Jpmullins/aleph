# Aleph design specs

The build proceeds in nine increments. Each increment ships its declared scope in **final production form** — no v1/v2 deferrals, no stubs. The top-level spec defines the architecture and the increment sequence; each increment spec is a concrete brief for one fresh coding-agent session.

## Read order

1. **Top-level (read first):** [`2026-05-26-aleph-design.md`](2026-05-26-aleph-design.md) — vision, architecture, domain model, UI, build increments, eval strategy, scope boundaries.
2. **Diagrams:** [`2026-05-26-aleph-architecture.excalidraw`](2026-05-26-aleph-architecture.excalidraw), [`2026-05-26-aleph-domain.excalidraw`](2026-05-26-aleph-domain.excalidraw), [`2026-05-26-aleph-ui.excalidraw`](2026-05-26-aleph-ui.excalidraw). Regenerate with `python3 _gen_excalidraw.py`.
3. **Increment briefs (build in order):**

| # | Spec | Scope summary | End state |
|---|---|---|---|
| 0 | [`2026-05-27-inc-0-foundations-design.md`](2026-05-27-inc-0-foundations-design.md) | Monorepo, Docker Compose, auth + `Principal`, append-only hash-chained `ActionLedgerEvent`, `ModelCall`/`CostLedgerEvent`/`Budget`, LiteLLM transport (the single LLM chokepoint), Langfuse + OTEL, seeded `aleph-dev` and `aleph-production` `ModelProfile`s. | One-command boot, project create + ledger event + cost ledger entry from a smoke LLM round-trip through the gateway. |
| 1 | [`2026-05-27-inc-1-rks-wiki-skeleton-design.md`](2026-05-27-inc-1-rks-wiki-skeleton-design.md) | RKS (`Source`/`SourceVersion`/`SourceAsset`/`NormalizedDocument`/`DocumentChunk`/`Connector`/`ConnectorBinding`), Upload connector, normalization + chunking + embedding (intra-source only), wiki entities (`WikiPage`/`Revision`/`Section`/`Link`/`Claim`/`Citation`/`SourcePage`/`Alias`/`HandEditMark`/`RejectionFeedback`/`WikiIndex`), wiki ingest agent as a 7-node LangGraph workflow with hand-edit + rejection-feedback wiring. | Drop a PDF → `SourcePage` + stub topic pages appear in the Wiki tab; aliases extracted; intra-source chunk search live. |
| 2 | [`2026-05-27-inc-2-wiki-first-chat-design.md`](2026-05-27-inc-2-wiki-first-chat-design.md) | `AssistantSession`/`Thread`/`Message`, wiki-first retrieval router (page-selector LLM + 1-hop wikilink expansion + composer), intra-source descent only when composer requests it, coverage-gap detection that flags synthesis (action lands Inc 3), SSE streaming, chat UI with `[[wikilink]]`/`[c…]` hover previews, cost badge, budget banner. | Chat with the wiki and get cited answers — the moment Aleph becomes Aleph. |
| 3 | [`2026-05-27-inc-3-aiq-connectors-synthesize-design.md`](2026-05-27-inc-3-aiq-connectors-synthesize-design.md) | NVIDIA AIQ vendored as submodule at `vendor/aiq` (LLM configs swapped to LiteLLM gateway), `ConnectorCredential` encrypted, full connector roster (Tavily, Exa, Serper, arXiv, Semantic Scholar, OpenAlex, RSS, HuggingFace; Lens.org disabled pending cred), AIQ tokenomics → CostLedger, citation_verification pre-flight integrated, `/synthesize` action: AIQ → wiki agent → owner approval → wiki content. | KB grows from real queries; cost still ledgered; AIQ has zero direct DB/S3 credentials. |
| 4 | [`2026-05-27-inc-4-a2ui-surfaces-design.md`](2026-05-27-inc-4-a2ui-surfaces-design.md) | A2UI Aleph Catalog v1.0.0 (5 Surface components + 12 inline cards), `@a2ui/react` integration, `InteractiveCard`/`Version`/`CardAction` models, `Note`/`NoteSection` models, `ActionRouter` as single dispatch chokepoint, schema validation on both ends. | The entire right panel is A2UI-rendered. Synthesis proposals appear as `ApprovalCard`s in Briefs. |
| 5 | [`2026-05-27-inc-5-reviewers-hypotheses-design.md`](2026-05-27-inc-5-reviewers-hypotheses-design.md) | `MechanicalReviewer` (LangGraph, auto-runs every revision, 7 parallel checks), `EditorialReviewer` (Deep Agents, scheduled + threshold, 5 sub-dimensions), `ApprovalRequest` wraps Inc 3's `ApprovalDecision`, `Hypothesis`/`Version`/`Evidence` with rule-driven confidence transitions, `AgentMemory`, full rejection-feedback UX. | Wiki revisions auto-validated; editorial findings flow into Briefs; hypotheses are first-class. |
| 6 | [`2026-05-27-inc-6-datasets-visualization-design.md`](2026-05-27-inc-6-datasets-visualization-design.md) | `Dataset`/`DatasetVersion` (immutable)/`Observation`, artificialanalysis.ai connector (first `dataset_rows`-output), full `ChartCard`/`TableCard`/`MapCard`/`GraphCard` impls bound to specific `DatasetVersion`s, cell-edit creates new version, cards pin to surfaces. | Charts bound to immutable dataset snapshots embed inside wiki/notes/briefs. |
| 7 | [`2026-05-27-inc-7-builder-artifacts-design.md`](2026-05-27-inc-7-builder-artifacts-design.md) | `RenderedAsset` + Playwright sandbox (no DB/S3 creds), `Artifact`/`Version`, Builder LangGraph workflow, exporters (PDF/DOCX/markdown-bundle/source-pack), CSL bibliography (APA-7/Chicago/IEEE/Vancouver/custom), full lineage_jsonb, reproducible builds. | Export a cited PDF with embedded charts; lineage traceable end-to-end. |
| 8 | [`2026-05-27-inc-8-eval-feedback-gates-design.md`](2026-05-27-inc-8-eval-feedback-gates-design.md) | `EvalDataset`/`Case`/`Run`/`Result`, unified runner, all per-increment evals discoverable from filesystem, `UserFeedback` model + inline affordances, AIQ FreshQA + DeepResearch Bench adapters, CI gates (permission leakage = 0, citation correctness, cost drift), feedback → eval-case promotion. | Self-monitoring product. Every PR runs the cross-cutting eval suite under both ModelProfiles. |

## Cross-references

Every increment spec includes:

- `Depends on` header naming prior increments
- `What downstream increments rely on` section
- `Handoff to Increment N+1` section pointing forward

If a coding agent picks up an increment, it should:

1. Read the top-level spec for context (or rely on its `MEMORY.md` summary).
2. Read the previous increment's `Handoff` section (which is the entry point comment).
3. Read this increment's spec.
4. Build per acceptance criteria.
5. Update `docs/implementation-log.md` with the entry template (see Inc 0 §0.17).
6. The next session picks up Inc N+1 with the prior log as context.

## Working principles recorded in these specs

- **Wiki-first retrieval.** The assistant queries the wiki (LLM-routed page selection + wikilink graph); RKS chunks reached only on descent. (Inc 1 §1.8, Inc 2 §2.4)
- **Provenance is structural, not editorial.** Append-only hash-chained `ActionLedgerEvent`. (Inc 0 §0.4.4)
- **Single LLM chokepoint.** All LLM/embedding calls through the Insights LiteLLM Gateway. (Inc 0 §0.8)
- **Agents call services; services own state.** AIQ has zero direct DB/S3 credentials. (Inc 3 §3.7)
- **A2UI declarative-only.** No agent-generated JavaScript ever executes. (Inc 4 §4.3)
- **No placeholder code, ever.** Each increment ships its scope production-complete; deferred features are explicitly out-of-scope or sequenced. (top-level §15.1, §16.1, §16.2)
- **Track upstream latest, verify versions at install time.** (top-level §15.6; reaffirmed in every spec)
- **Immutable revisions everywhere they matter.** Action ledger, wiki revisions, dataset versions, artifact versions, interactive card versions, hypothesis versions, rendered assets. (passim)
