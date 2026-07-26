# Aleph: Current State and the Path to a Web of Belief

Deep analysis, 2026-07-26. Basis: `jpmullins/aleph` at `bcc478a` (2026-07-06), four parallel subsystem audits (data model, research/ingestion, trust/epistemics/evals, frontend), plus a comparative read of `UMD-ARLIS/benchmesh_v2` for UI/UX. Findings are marked **[V]** verified by reading cited code, **[E]** verified by executing code, **[I]** inferred. I independently re-verified the central finding in §1 before writing.

---

## 0. Executive summary

Aleph is architecturally the right system. The package DAG, the ledger discipline, the approval gates, the A2UI delta substrate, the trust-layer design — these are good, and measured against the mid-2026 commercial landscape, no shipped product has this shape. The problem is not the architecture.

**The problem is that the architecture's central join key is never written.** `Citation.source_page_id` is `NULL` and `Citation.chunk_ids` is `[]` at every production write site **[V]**. Every subsystem that walks `Claim → Citation → SourcePage → Source` — freshness citation-health, source-freshness, the retraction blast radius, the refresh fact-diff, mechanical review's stale-source and DOI-retraction paths — returns empty on real data. The e2e tests pass because two fixtures hand-construct the missing link. Four documents' worth of trust guarantees rest on a column that two code comments promised someone else would fill in.

The consequence compounds: because ungrounded pages score *higher* on freshness than grounded ones (§1.2), the system as deployed rewards content with no claims. The native research loop, which emits `claims=[]` outright **[V]**, produces the least-grounded content in the system and receives the highest trust score.

This changes the roadmap's shape. The claim-model migration I recommended in the prior assessment is still necessary, but it is step 4, not step 1. Steps 1–3 are repairs measured in days that revive several already-built subsystems. Doing the migration first would mean porting a hollow provenance chain into a better-shaped schema.

**Sequence:** repair the chain (§6.1, days) → make provenance exact (§6.2, ~2 weeks) → UI shell from benchmesh (§6.3, ~3 weeks, zero backend dependency, runs in parallel) → promote claims to first-class nodes (§6.4, the big one) → typed edges and decomposed status (§6.5–6.6) → scale work only when measured (§6.7).

---

## 1. The critical finding and its blast radius

### 1.1 The chain is severed at one column

Exhaustive enumeration of every non-test assignment **[V]**:

| Site | Value |
|---|---|
| `wiki_service.py:337` | `source_page_id=cite.source_page_id` — the only INSERT, copies the draft verbatim |
| `agent/workflow.py:421-422` | `chunk_ids=[]`, `source_page_id=None,  # Set in commit step (self-citation).` |
| `synthesis_workflow.py:219-220` | `chunk_ids=[]`, `source_page_id=None,  # source page resolved by short_id later` |
| `curator_service.py:778, :845` | faithful carry-forward of `None` |

**Both deferral comments name steps that do not exist.**

The commit step in `agent/workflow.py:574-637` *does* create the `SourcePage` bridge row at `:609-637` — and never returns to update the `Citation` rows it created seconds earlier. The bridge is built; nothing is routed across it.

Worse, in `synthesis_workflow.py:213-223` the resolved reference is in scope and discarded three lines before it is needed:

```python
ref = report.citations_by_marker.get(marker.strip("[]"))   # ResearchSourceRef, has .source_short_id
if ref is None:
    continue
citations.append(CitationDraft(chunk_ids=[], source_page_id=None, ...))  # ref never used again
```

`ref` is bound purely as a null-guard. `report.citations_by_marker` (`research_workflow.py:329`) is a real, verified marker→source map that `_node_citation_verification` (`synthesis_workflow.py:129-152`) checks every `[cN]` against and can block the commit on. **The system verifies provenance in memory at compose time and then declines to persist it.**

The research path never gets that far: `build_report()` hardcodes `claims=[]` (`research_workflow.py:337`), and `packages/aleph-research/tests/test_report.py:91` *asserts* the empty output as correct behavior **[V]**. Everything the native research loop produces lands with zero `WikiClaim` rows and zero `Citation` rows.

Only two `CitationDraft`s in the repo carry a real `source_page_id`: `tests/e2e/test_retraction.py:114` and `tests/e2e/test_wiki_refresh.py:146`, each hand-building the bridge that production builds but never links **[V]**.

### 1.2 What each trust mechanism actually computes

| Mechanism | Verdict | Detail |
|---|---|---|
| `compute_freshness` pure fn | **CORRECT** | Well-tested; the break is in its caller, `curator_service.py:364-391`, which has zero test coverage **[V]** |
| Recency (0–25) | **CORRECT** | No citation dependency |
| Citation health (0–25) | **INERT + INVERTED** | Reads `source_ids` (always empty) → **0** for any page with claims; but `if not citations: return 25` (`freshness.py:75-77`) → **25** for pages with none |
| Source freshness (0–25) | **INERT** | `source_versions` unconditionally `[]` → hardwired **0**. The 0–100 score has a production ceiling of 75 |
| Verification (0–25) | **DEGRADED** | Tautologically 25: fallback branch tests `confidence == "cited"`, which `agent/workflow.py:417` hardcodes |
| `retract_source` source marking | **CORRECT** | Status, timestamp, ledger event all work |
| `retract_source` blast radius | **INERT** | INNER JOIN on a NULL column (`retraction.py:74-79`) → zero claims flagged, ever |
| Page `retracted` badge/banner | **UNREACHABLE UI** | Derived from `WikiClaim.confidence == "retracted"`, which nothing sets |
| `wiki_refresh_job` | **INERT** | `contributing` always empty → no fetch, no fact-diff LLM call, ever |
| `refresh_stale_pages_job` | **DEAD CODE** | Zero enqueue callers; `WorkerSettings` has no `cron_jobs` key; grep for `cron_jobs|CronJob|@cron` returns nothing **[V]**. `docs/wiki.md:44` describes a scheduler that does not exist |
| Artifact/brief `drifted` | **CORRECT** | Rides revision identity, not citation provenance — the existence proof that the rest could work |

**The headline consequence: freshness is anti-correlated with groundedness.** An ingested source page carrying real extracted claims scores 50. A synthesis/research page with `claims=[]` scores 75, because the vacuously-healthy branch is the only way to earn that dimension in production. And `_STALE_FRESHNESS_MAX = 60` (`wiki_refresh.py:56`) sits precisely between them, so the staleness sweep — if it were wired — would target exactly the grounded pages and never the ungrounded ones.

**Retraction is also unreachable, independently of being inert.** `POST /v1/projects/{pid}/sources/{sid}/retract` (`routes/sources.py:318`) has zero callers repo-wide — no UI, no test, no script **[V]**. The other documented trigger, the mechanical reviewer's `doi_verification` node, resolves sources through `_registry_sources` (`mechanical/workflow.py:164-179`), which uses the same broken join and always returns `[]`. `docs/wiki.md:48` calls this "the single path all retraction triggers funnel through." Both tributaries are dry.

One more, quietly bad: the `retracted_source` ReviewFinding is still emitted, with a description reading verbatim *"0 dependent claim(s) across 0 page(s) flagged retracted/contested"* (`retraction.py:184-185`) **[V]** — the system announces its own emptiness in the UI. And because the catalog severity enum is `["info","low","medium","high"]` with no `critical` (`catalog.py:229`), that finding renders slate grey, visually calmer than a `medium` **[V]**.

### 1.3 Why this survived review

The two comments. `source_page_id=None  # Set in commit step` and `# source page resolved by short_id later` both read as *intentional and handled* rather than *broken*. Everything downstream was built, tested against fixtures that supply the missing value, and closed through adversarial review — because the review checked the Final State's falsifiable statements, and every one of them is satisfiable with hand-seeded data.

The generalizable lesson for GOAL.md's process: **exit criteria must include at least one assertion against data produced by the production path, not by a fixture.** "Ingest a real source, then assert `SELECT count(*) FROM citations WHERE source_page_id IS NOT NULL > 0`" would have caught this on day one of WP-6.

---

## 2. Provenance substrate: also numerically wrong

Independent of the NULL join, the layer beneath it cannot support quote-level provenance today.

`DocumentChunk.char_start/char_end` exist (`aleph-rks/models.py:149-150`) but **do not index the normalized markdown** **[E]**. Verified by execution: `markdown[char_start:char_end] != chunk.text` for 7 of 12 chunks of the repo's own README at `target_tokens=120`, and 8 of 8 chunks of a line-wrapped synthetic paper, with monotonically accumulating drift (−1, −4, −7, −10, −14, −17, −20, −23 chars).

Root cause (`chunking.py:129,136,144,149,156,164,169,176`): `chunk_markdown` stores `char_end = buf_start + len(text)` where `text = " ".join(buf).strip()` is a whitespace-normalized re-join, **not a source substring**, then re-bases the next chunk with a normalized-space heuristic.

Second defect, load-bearing for any Claimify-style pipeline whose *select* stage is sentence-atomic: the sentence splitter `_SENTENCE_END = r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9])"` (`chunking.py:29`) shatters on scientific abbreviations **[E]**. `"Fig. 3"` → `["Fig.", "3 the effect holds."]`; `"Smith et al. 2020"` → `["Smith et al.", "2020 report..."]`; same for `"vs. 5%"`, `"Eq. 2"`.

So the ordering is forced: **fix the chunker before writing spans**, or you persist provenance that points at the wrong characters — which is worse than none, because it looks trustworthy.

---

## 3. Epistemic layer: the computation is unreachable

`aleph_hypotheses.confidence` is the only computed-status function in the system, and my earlier criticism of it was correct but incomplete. The audit found a harder defect.

**`WELL_SUPPORTED` cannot be reached.** The guard at `confidence.py:62` requires `max_pos >= 1.5`. Every call site passes `weight=1.0` — `hypothesis_service.py:181`, `routes/hypotheses.py:59`, `copilot_agent.py:543`, `subagents/analyst.py:64` — and no code path anywhere computes or derives a weight **[V]**. A hypothesis with 100 supporting items and zero contradictions classifies as `weakly_supported`. Meanwhile `refuted` needs only three default-weight contradictions. **Disconfirmation is cheap and terminal; confirmation is permanently capped.**

Corroborating evidence that nobody has observed the state: the UI tone map `HypothesesSurface.tsx:17-24` has keys for `supported` and `initial` — neither is a legal `Confidence` value — and omits `well_supported` entirely **[V]**.

Additional verified defects: it is not a state machine (never reads current state, so `refuted → well_supported` is legal); `contextualizes` rows are silently discarded; double-counting is structural (no grouping key, no `source_id` column, no unique constraint on `(hypothesis_id, target_id)`) *and invisible* because `HypothesisMatrix.tsx:50` uses `cells.find(...)`; balanced strong evidence (`pos=10, neg=10`) reads identically to no evidence; there is no `remove_evidence` and no way to retract; `EvidenceRow` carries only `(stance, weight)` so the function **cannot see time**; the threshold citation "spec §5.5" points at a section titled *EditorialReviewer* **[V]**; and no UI can attach evidence at all — `build_action_router()` registers 20 actions and has no `attach_evidence` **[V]**. The package has no `tests/` directory, the only domain package without one.

Nothing auto-attaches evidence from wiki claims, findings, or research: grep for `hypothes` across `aleph-research`, `aleph-wiki`, `aleph-rks`, `apps/workers` returns zero **[V]**.

**Read positively:** `HypothesisEvidence` (`aleph-hypotheses/models.py:62-72`) is already a migrated, typed, weighted, three-valued stance edge, and `evidence_kind` already admits `"claim"`. The substrate for claim-relation edges exists. It is pointed at the wrong node type and fed by nothing.

---

## 4. Other subsystems, honestly

**Reviewers.** MechanicalReviewer's deterministic nodes mostly work, but `stale_sources` is inert (same NULL join), `broken_links` silently drops every wikilink appearing exactly once via an undocumented `occurrences > 1` filter (`mechanical/workflow.py:357-361`), and `duplicate_sources` runs project-wide on every commit with no dedup, so Briefs accumulates N identical cards per ingest forever. The docstring claim "runs on every wiki revision commit" is false: of eight `commit_revision` call sites, only the two inside `wiki_ingest` enqueue a review **[V]** — synthesis-approved pages, the research loop's entire output, are never mechanically reviewed.

EditorialReviewer has two blocking bugs. `finding_count` is always 0 for every editorial run, because the nodes write to state keys (`n_c`…`n_f`) absent from `EditorialReviewState` — and LangGraph silently drops them, a failure mode this codebase documents at `mechanical/workflow.py:95-97` and previously fixed in the mechanical twin **[V]**. Pyright flagged it; five `# type: ignore` comments silenced it. Second: `ReviewRun.trigger` is `String(32)` while the sole production caller builds `f"assistant: review [[{page_title}]]"` — 22 literal characters, so **any page title over 10 characters overflows the column** and the job raises before the graph runs **[V]**. The API returns `202 {"enqueued": true}` regardless.

All five editorial nodes see the same payload, and it is only page prose truncated at 4000 chars — `_sample_payload` never queries `WikiClaim`, `Citation`, `Source`, or `WikiLink` **[V]**. Three of the five prompts therefore ask for data that is never supplied (`weak_source` asks about source quality with no sources in the payload; `factual_freshness` asks about dates with no dates; `coverage_gap` asks about wikilinks with no links). The contradiction node's schema offers `evidence_refs: [{"kind": "claim", ...}]` while no claim id is ever in the payload — **the system invites the model to hallucinate exactly the provenance the platform exists to guarantee**, and persists it unvalidated.

**Evals.** Scaffolding. Executed: 4 datasets, **6 cases**, 800 bytes total, all pass, exit 0 **[E]**. Every row carries its own `actual` field hand-written to match `expected`; no code path populates `actual` **[V]**. `aleph_permissions.jsonl` asserts `leaked_targets: []` — a literal the author typed, with no permission check performed. The runner's docstring claims it writes `EvalRun`/`EvalResult` rows; it imports no session and the tables are permanently empty **[V]**. `baselines.json` is read by nothing. The gate is misdocumented and effectively binary: probes confirmed a `warning`-kind dataset still exits 1, and per-profile thresholds can only add failures, never tolerate them — **the effective threshold for every dataset is 100%** **[E]**. With the committed fixtures the gate is arithmetically incapable of failing.

**Scale.** Bottleneck ordering, with the mitigating detail that matters:

| Scale | First failure |
|---|---|
| ~10k docs | The arq worker: 4 concurrent slots × 8–29 LLM calls per doc in `wiki_ingest_job` ≈ days. Also `_next_short_id` does `SELECT count(*) FROM sources` with **no project filter** on every registration (`source_service.py:36-39`) |
| ~100k docs | The Action Ledger. `_lock_or_create_head` takes `SELECT … FOR UPDATE` on one row per project, held until commit — and in `chunk_embed_job` that transaction also bulk-inserts every chunk (`chunk_embed.py:192-258`), so lock hold time scales with document size and all project writes serialize. Separately, `verify_project_chain` loads every event into Python memory (`ledger.py:194-202`) and is unrunnable well before this |
| ~1M docs | The HNSW index: 40M × 1024-dim float4 ≈ **164 GB** of raw vectors in an unpartitioned table on a 1.5 GB-limit container. **But every query is `WHERE source_id = ?`** (`retrieval.py:60-66`) — intra-source descent only — so the ANN index is never used for its ANN properties. A btree on `(source_id, ordinal)` plus exact scan serves the current access pattern. The largest object in the system is not needed by the current design |

Also unbounded with no partitioning or retention: `model_calls`, `cost_ledger_events`, `document_chunks`, `wiki_revisions` (full body per revision), and `action_ledger_events` — which is immutable by trigger and therefore **cannot be pruned at all** **[V]**.

**Schema hygiene.** No table in the repo uses `ForeignKey`; zero `CHECK` constraints anywhere **[V]**. Every relationship is a bare unconstrained UUID and every enum-like field is free text. This is why a NULL join key produced empty results instead of an error, and it is the single practice most worth reversing on new tables.

---

## 5. What is genuinely strong

Stating this precisely, because §1–4 is a long list of defects and the correct conclusion is "repair," not "rewrite."

The A2UI delta substrate is correct: server-built surfaces, data-model bindings, `updateDataModel` deltas over SSE woken by LISTEN/NOTIFY, resume/ordering semantics, and a CI-enforced no-self-fetch rule. The `ActionRouter` with per-action ledger audit is the right cross-surface channel. The `code_runner` isolation posture (cap_drop ALL, read-only rootfs, no credentials, internal-only network) is sound. `drifted` works. The hash-chained ledger with immutability triggers is real. `aleph-scholar` is pure-HTTP with zero LLM calls and gives tri-state DOI verification with retraction detection — genuinely better than the spec asked for. CopilotKit eyes-and-hands (`useAgentContext` / `useFrontendTool`) is the right pattern.

And the reusable substrate is larger than it appears:

- `HypothesisEvidence` — a migrated typed weighted stance edge (§3)
- `SourcePage.extracted_claims_jsonb` — one lossy writer, **zero readers**; free real estate for the layer-1 source frame
- `NormalizedDocument.structure_jsonb` — written, never read; the natural home for a page-offset map, no migration needed
- `Citation.chunk_ids` — the quote-provenance wire format already exists end-to-end, surfaced by `_resolve_citations` (`surfaces.py:524-531`) and `ClaimCard` (`catalog.py:104`); only the writer passes `[]`
- `RejectionFeedback` → prompt injection (`agent/workflow.py:363-380`) — a working LLM-proposes/human-verifies loop, already built, currently applied only to page composition
- `ReviewFinding.target_claim_id` — the finding→claim edge, migrated, never written
- `Capability.RERANK` — pre-declared, bound nowhere; a slot for claim-cluster reranking
- `AliasService` — surface-form→canonical resolution with confidence; adding a `purpose` discriminator turns it into the purpose-relative terminology map

---

## 6. Roadmap

Ordering principle: repairs that revive built subsystems first; then exactness; then the shell (zero backend dependency, parallelizable); then the claim model; then edges and status; scale last and only on measurement.

### 6.1 Repairs — days, not weeks

1. **Resolve `Citation.source_page_id` at commit.** Two edits. In `agent/workflow.py`, after the `SourcePage` bridge is created at `:609-637`, UPDATE the `Citation` rows for the just-committed revision to point at `sp.id` — the "self-citation" the comment at `:422` already promises. In `synthesis_workflow.py:213-220`, stop discarding `ref`: resolve `ref.source_short_id → Source → SourcePage.id`. **This one change revives, at once:** citation health as a real gradient, source freshness (restoring the score's top quartile), `stale_sources`, the DOI-verification retraction branch, the refresh fact-diff, and the entire retraction blast radius including the currently-unreachable page badge and banner.
2. **Make the research loop emit claims.** `research_workflow.py:337` plus `test_report.py:91`, which pins the empty output as expected.
3. **Fix the freshness vacuous-healthy branch** (`freshness.py:75-77`). A page with no claims must not outscore a page with cited ones. Add the missing unit test for `curator_service._recompute_freshness`.
4. **Two one-line reviewer fixes:** add `n_c`…`n_f` to `EditorialReviewState`; widen `ReviewRun.trigger` past `String(32)`. Without the second, editorial review cannot start.
5. **Wire the scheduler or delete it.** No `cron_jobs`, no caller. Either add the cron or remove the job and correct `docs/wiki.md:44`.
6. **Make retraction reachable** — add a UI affordance or scholar auto-detect hook, once (1) makes the blast radius non-empty.
7. **Add `critical` to the severity enum and renderer tone map**, and call `validate_component` on server-built surfaces. Ledger `review_finding.create` and add it (plus `source.retract`, `wiki_claim.retract_flag`) to the live-signals allowlist so new findings actually push a surface update.
8. **Amend GOAL.md's exit-criteria rule:** every work package must assert at least once against data produced by the production path, not a fixture.

### 6.2 Exact provenance — ~2 weeks

9. **Offset-exact chunker + abbreviation-safe sentence splitter** (`chunking.py:29,103-181`). Demonstrably wrong today; every quote, span, locator, and Claimify *select* stage sits on it. Add a property test: `markdown[chunk.char_start:chunk.char_end] == chunk.text` for every chunk.
10. **Populate `Citation.chunk_ids`; add span columns** (`quote_start`, `quote_end`, `quote_sha256`) so a claim resolves to a verbatim substring with a locator, and the quote can be re-verified against the source later.
11. **One Pydantic `Claim` schema in `aleph-core/schemas/`.** Five incompatible shapes exist today; consolidate before adding a sixth.
12. **Capture source-quality signals** — `venue`, `cited_by_count`, `is_retracted`, OA status — in `WorkRef`/`DoiVerdict` and persist them. The records are already downloaded in full; none of these fields currently reach Postgres, and they are the required inputs to any decomposed status computation.
13. **Tiering:** sha256 dedup + a triage gate at `normalize.py:193-194`, and enable prompt caching in `client.py:223-233` (the 90% discount is already priced and already parsed; only the request flag is missing). Do this before the 4-stage extraction pipeline multiplies cost against an unfiltered corpus.

### 6.3 UI shell — ~3 weeks, zero backend dependency, run in parallel

The defining requirement of a belief UI is **comparative reading under provenance**: two claims, their evidence, and their consequences visible at once. Aleph's 30%-wide single-column tabbed panel structurally cannot do that, and no new cards fix it. benchmesh solved the shell problem with a better design system and a much worse protocol. **Take benchmesh's shell, keep Aleph's protocol.**

Port: the token system and pre-paint theme boot (`globals.css:6-82`, `layout.tsx:14,20`) — semantic tokens, hand-tuned dark reciprocals rather than inversion, plus the two global rules Aleph lacks outright (`:focus-visible`, `prefers-reduced-motion`); this deletes Aleph's `!important` dark-mode shim. The card shell as a **capability contract** (`card-shell.tsx:20-202`) — eyebrow, pin, minimize, optional `savable` — which makes `pin_to_brief` an analyst gesture instead of an agent-only verb. The rail (`rail.tsx:9-21`) replacing the 5-tab bar, which is the hard ceiling on surface count (the belief UI needs 8+). The context bar (`context-bar.tsx:18-58`), rendering exactly the `useAgentContext` payload Aleph already computes and never shows the human. The staged pipeline strip for adjudication flows. `lifecycle.tsx`'s `groupByStatus` + `ValidationReportView`, and especially `workflow-review.tsx:260-265`'s inline explainer of what each button will do — for dispute adjudication, "what happens if I approve this" is the entire job. And `charts.py`'s epistemic rendering conventions (muted grey when a CI crosses zero) into Aleph's existing viz pipeline.

Port with modification: a rail-driven **tiled** 1/2/3-pane region over `react-resizable-panels` (already a dependency) — not benchmesh's free-floating canvas, which would drop delta streaming and ordering guarantees.

Do not port: the untyped `window` CustomEvent bus; localStorage as source of truth for sessions; the 158-line A2UI renderer (no deltas, no actions); regex-parsing agent prose for directives; cards fetching their own data; Next.js.

Also in this phase, independent of benchmesh: **URL state** for `{tab, page_id, claim_id, card_id}`. A belief graph requires stable links to a claim, an edge, a dispute. Do this before building the dispute queue, not after.

### 6.4 Claims as first-class nodes — the big one

14. **Stable claim identity.** Add `claim_key = sha256(normalized_text)` scoped to page, and in `commit_revision` match prior-revision claims on it and reuse their `id`. This is the minimum viable fix and is cheap. The fuller version — a `Claim` entity independent of page and revision with its own version history, where `WikiClaim` becomes a *rendering* of a claim at a revision — is the right destination; the `claim_key` step is a safe intermediate that unblocks edges immediately. Note `CuratorService._carry_claims` already copies text/confidence/citations forward faithfully; it just mints new ids.

Without this, **any claim-edge table decays to garbage within one curate cycle**, because every curator cross-link pass recompiles the page and re-mints every claim id.

### 6.5 Typed edges and adjudication

15. **`claim_relations` table** — `src_claim_id`, `dst_claim_id`, `kind ∈ {assumes, contradicts, refines, supersedes, duplicates}`, `confidence`, `rationale`, `detected_by`, `review_finding_id`, `provenance_jsonb`, `status ∈ {proposed, confirmed, rejected}`, plus `project_id`/`access_scope`/`ledger_event_id` per the invariants. Model it on `HypothesisEvidence`, which already has this shape. **Give this table real foreign keys** — the total absence of FKs is precisely why §1 failed silently, and it is the mistake not to repeat on new tables.
16. **Contradiction node proposes edges instead of orphan findings.** `_sample_payload` grows a claim-level variant joining through the now-populated citation chain; both ids validated against the loaded claim set and unrecognized ones dropped — the opposite of today's silent `except: None`.
17. **Dispute queue with human gating.** The `ApprovalCard` dispatch spine is the right machinery: single chokepoint, uniform ledger + `CardAction`, a correct `SELECT … FOR UPDATE` re-entrancy template in the `agent_action` branch, effect-before-status ordering, and a mandatory rejection reason — exactly the signal a dispute queue needs as training data. What must change: a `claim_edge` target_kind; a two-claim side-by-side renderer (`diff_card_id` at `catalog.py:213` is the intended hook, with no producer); a write-back handler with real effect (a confirmed `contradicts` edge should set both claims `contested`); and — before this becomes ground truth — resolving the role-gate inconsistency, where the UI path requires EDITOR and the unreachable route requires OWNER. At scale, add pagination (the queue is currently an unbounded UNION with no LIMIT), a dedup key, bulk action, assignment, and confidence-ordered prioritization; least-confident-first ordering is the main reason to build such a queue and is absent.

Consistent with the external evidence: dispute-class edges stay human-gated until our own eval shows a current frontier model clearing a measured precision bar, and even then auto-confirmation applies to the edge, not to the status change.

### 6.6 Decomposed, calibrated status

18. **Keep `next_confidence_from_evidence` byte-for-byte as a named baseline.** Add `grade.py` with a pure `assess_confidence(evidence, *, now, overrides) -> GradeAssessment` carrying `overall`, `overall_score`, and per-dimension `DimensionScore(dimension, machine_score, machine_reason, human_score, human_reason)` where `effective = human_score if not None else machine_score`. Six dimensions, each fixing a named defect from §3: `source_quality` (needs 6.2's persisted signals), `independence` (needs a new `source_id` on `HypothesisEvidence` — **without it the double-counting defect is unfixable**), `directness`, `consistency` (magnitude of disagreement, not just the residual), `precision`, `recency` (`now` injected, never read inside).
19. **Shadow-run both and ledger the comparison.** One `hypothesis.confidence.assess` event per computation with `{baseline, grade, overall_score, evidence_digest}`. Agreement rate, disagreement direction, and the divergent evidence sets then fall out of a single query over the hash-chained ledger, with no separate instrumentation. The grade stays advisory until an eval says otherwise.
20. **Fix the eval harness enough to measure this.** Per-item retention in `RunResult` (today each case is reduced to `(ok, score)` and the prediction is discarded, so ECE is not expressible); runner→DB persistence (`EvalResult` already has `actual_jsonb`/`diff_jsonb`/`score` — exactly the right shape, never written); a numeric confidence on claims to calibrate against (today `String(16)` categorical, hardcoded — **there is nothing to calibrate**); a text-in→claims-out entry point (the extractor is currently reachable only through the full ingest pipeline); and a gate that can express "≥ threshold" rather than 100%, since a calibration metric is never 100% and would permanently red the build.

   The adversarial cases to seed first: five items from one source, perfectly balanced evidence, contextualizes-only, retracted-source, and twenty supports at default weight. That last case alone would have caught the unreachable-`WELL_SUPPORTED` bug on day one.

### 6.7 Concept layer, then scale

21. **Concept + purpose-relative mappings**, extending `Alias` with a `purpose` discriminator and a `TermMapping` recording the reading adopted and what it drops. Real, but it only pays off once claims are stable nodes.
22. **Scale work, on measurement only.** In likely order of forcing: move the ledger append out of the chunk-insert transaction; make `verify_project_chain` streaming; scope `_next_short_id` to project; scoped/incremental surface recompute (today every LISTEN/NOTIFY wake rebuilds the whole tab); windowed bound collections. Reconsider the HNSW index against the actual access pattern before paying for it.

**On Neo4j:** nothing above requires a graph database. Typed edges and grounding-tree traversal are recursive CTEs over `claim_relations` in the Postgres 18 you already run (PG14+ `CYCLE`/`SEARCH` clauses are available). Postgres keeps the ledger, project scoping, and Alembic discipline the invariants depend on. Revisit when traversal depth or path queries are a measured bottleneck — and note that Kuzu, the obvious embedded alternative, was abandoned by its vendor in October 2025.

---

## 7. What I'd argue with

**`next-steps.md` is aimed at the wrong layer.** It is entirely UI/agent-framework work: borrowing open-analyst aesthetics, deeper CopilotKit/Deep Agents integration, unlocking A2UI, background agents, wiki bootstrap progress. §6.3 says some of that is right and should happen in parallel. But none of it addresses a severed provenance chain, an inert trust layer, an unreachable confidence state, or a tautological eval gate. Building more surfaces over hollow data increases the surface area of the illusion.

**The bigger risk is epistemic, not technical.** Aleph currently displays freshness scores, confidence labels, and trust badges computed from empty inputs. A system that reports "well-sourced" from a join that returns zero rows is worse than one that reports nothing, and this is a system whose entire purpose is auditable belief. §6.1 is therefore not a cleanup backlog; it is the thing that makes every existing claim the UI makes true.

**The good news, stated plainly.** Every defect in §1–4 is a wiring defect, not a design defect. The tables exist, the services exist, the triggers exist, the UI exists. The estimate that Aleph is ~80% of the engine still stands — the correction is that the missing 20% is load-bearing and sits at the bottom, not the top.

---

## 8. Deliverables from this analysis

Four full subsystem reports back this document and contain the detail it compresses (file:line for every claim, complete schema inventories, per-node prompt analysis, the full catalog roster, the benchmesh comparison matrix). They are available on request as separate files:

- Data model + claim lifecycle + migration blast radius (60-table inventory, proposed edge schema)
- Research loop + ingestion + scholar + scale (node-by-node, the chunker execution evidence, bottleneck ordering)
- Trust layer + reviewers + hypotheses + evals (the adversarial audit; §1–4 draws most heavily on this)
- Frontend + A2UI + benchmesh comparative (full catalog roster, port/don't-port list, UI phase plan)
