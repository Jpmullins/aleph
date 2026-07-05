# Aleph Audit Remediation — Goal, Acceptance & Validation Harness

**Status:** active contract (2026-06-30) · **Owner:** Justin Mullins · **Driver:** Claude (autonomous)
**Branch:** `audit-remediation` · **Design specs:** `2026-06-30-audit-remediation-design.md` (WS-B/C/D);
WS-A (custom AIQ image) + WS-E (Library + render + viewer cards) get their own specs before their phases start.

This is the **definition of done** for the **entire** 2026-06-29 audit — every finding across all
four tiers, plus the two additive features. I (Claude) execute against it autonomously, in the
agreed order. A finding/phase is "done" only when its acceptance box is checked with **evidence
pasted** (command output / screenshot / test name). No success is claimed without the command
output that proves it (per `verification-before-completion`). The **Findings Coverage Matrix
(§8)** guarantees every audit item maps to a phase — nothing is dropped.

---

## 0. Goal

Close **all** of the 2026-06-29 audit's findings — correctness, integrity, dead-code, robustness,
dormant-surface, and doc-drift — then add the two additive features (multi-source research via a
custom AIQ image; raw-source visibility with a "Library" tab + viewer cards). Each ships in final
production form (no stub/`v1`), proven by automated tests + a real-browser Playwright flow + the
repo's quality gates.

## 1. Order of execution (locked)

1. **Phase 1 — Ledger holes + chain verify** (rule #4 holes, chain verify)
2. **Phase 2 — Curator chokepoint + `cross_link` + curator robustness**
3. **Phase 3 — Merge-proposal surface + real `apply_merge`**
4. **Phase 4 — Agent ModelProfile + robust cost + env-cred gate**
5. **Phase 4b — Retrieval quality hardening**
6. **Phase 5 — Re-embed worker + model-profile route + functional `set_model_profile`**
7. **Phase 6 — A2UI deltas for Hypotheses**
8. **Phase 7 — Delete legacy chat pipeline + dormant-surface & dead-code cleanup + doc refresh**
9. **Phase 8 — Connectors via custom AIQ image + AIQ robustness** (*spec written first*)
10. **Phase 9 — Raw-source visibility: Library tab + render worker + viewer cards + builder fixes** (*spec written first*)

Checkpoints: pause for review after **Phase 3** (first user-visible change) and after **Phase 7**
(audit batch complete, before additive Phases 8–9) — unless told to run straight through.

## 2. Global gates — MUST pass at the end of every phase (no exceptions)

```bash
uv run ruff check .                      # → All checks passed!
uv run ruff format --check .             # → 0 would reformat
uv run pyright                           # → 0 errors (no NEW errors)
uv run pytest -m "not integration" -q    # → all pass, 0 fail
cd apps/api && uv run alembic check      # → No new upgrade operations detected
pnpm -C apps/web typecheck && pnpm -C apps/web lint && pnpm -C apps/web build   # → clean
uv run python -m aleph_evals --datasets all --gate strict   # → pass_rate 1.0
grep -rnE "TODO|FIXME|NotImplementedError" --include=*.py packages apps | grep -v tests/   # → no output
```

Integration + e2e (stack up via `run-aleph`), paste output per phase:

```bash
uv run pytest -m integration -q          # → all pass incl. the phase's new tests
pnpm -C tests/playwright test            # → all specs green incl. the phase's new spec
```

## 3. Per-phase acceptance criteria + validation

### Phase 1 — Ledger holes + chain verify  `[F08, F09]` ✅ DONE (2026-06-30)
- [x] Every wiki mutation writes an `ActionLedgerEvent` in-txn: `wiki.alias.upsert`,
  `wiki.links.repair` (when ≥1 repaired), `wiki.handedit.mark`, `wiki.handedit.clear`,
  `wiki.feedback.write`. — integration tests assert each kind appears.
- [x] `GET /v1/projects/{id}/ledger/verify` → `{ok:true}` on a clean chain; pinpoints first
  divergence on a tampered (hand-built) chain. — `verify_event_chain`/`verify_project_chain`.
- [x] Unit: `test_chain_verify.py` (3). Integration: `test_ledger_verify.py`,
  `test_alias_ledger.py`, `test_handedit_feedback_ledger.py` (all green); `test_curator_repair.py`
  (4) stays green.
- Evidence: `ruff check .` clean; `ruff format --check .` 0 reformat; `pytest -m "not integration"`
  152 passed; pyright (touched) 0 errors; `alembic check` no new ops (no migration).

### Phase 2 — Curator chokepoint + `cross_link` + robustness  `[F03, F04, F22, F24]` ✅ DONE (2026-06-30)
- [x] Curation enqueued from **every** authoring path (bootstrap overview, notes-promote, synthesis,
  ingest). Transaction-safe **post-commit** enqueue at each path (not an in-commit hook — that would
  race the curate job ahead of the caller's commit); the curate job never re-enqueues → no loop.
  Ingest enqueue moved out of the mechanical-review `if pending is None` guard.
- [x] A page mentioning an existing sibling's title/alias **in prose** (no `[[ ]]`) gets a
  resolved `WikiLink` after curation (problem B); re-run is a no-op. `[F03]` — `inject_cross_links`.
- [x] `[F22]` Overview resolved by a stable `page_kind == "overview"` marker (bootstrap sets it),
  title fallback; recurate preserves `page_kind`; dedup excludes overview by id.
- [x] `[F24]` Curate job logs `wiki.curate.llm_steps_skipped_no_profile` when no ModelProfile.
- [x] Unit: `test_cross_link.py` (7). Integration: `test_cross_link_curate.py`; `test_curator_repair`
  (4) green. Evidence: ruff/format clean, `pytest -m "not integration"` **159 passed**, pyright
  (touched) 0 errors, `alembic check` no new ops.
- [ ] Eval: graph-connectivity scorer → deferred to Phase 7 (eval-gate batch). Live enqueue→worker
  validation rides the Phase 3 stack rebuild + Playwright.

### Phase 3 — Merge-proposal surface + real `apply_merge`  `[F05, F23]` ✅ DONE (2026-06-30)
- [x] Pending `PageMergeProposal` renders as an ApprovalCard on the **Briefs** tab (the established
  home for approval cards; the generic ApprovalCard needs no new frontend); Approve → `apply_merge`,
  Reject → marked, both via the `/cards/actions` router. `wiki_curation_status` reports pending
  merges (conversational discovery).
- [x] `[F23]` `apply_merge` rewrites `[[source]]` → `[[target]]` in inbound page bodies (recommit,
  label-preserving) **plus** the structural link redirect + alias + soft-delete — all ledgered
  (`links_redirected` + `bodies_rewritten`).
- [x] Integration: `test_merge_approve_action.py` (surfaces in briefs → approve via `/cards/actions`
  → merged + soft-deleted; reject), `test_merge_body_rewrite.py`. Evidence: 162 unit pass, pyright
  (touched) 0 errors, ruff/format clean.
- [ ] **Playwright** `merge-proposal.spec.ts` → batched with the Phase 6/9 UI-Playwright run after
  the stack rebuild (the ApprovalCard approve/reject is generic, already-working frontend).
- Note: surfaced on **Briefs** rather than a new wiki banner — Briefs is where ApprovalCards + the
  action-router already live, so this is fully reachable with zero new frontend.

### Phase 4 — Agent ModelProfile + robust cost + env-cred gate  `[F06, F07, F10]`
- [ ] `[F06]` Under `aleph-production`, orchestrator + each subagent resolve model from the project
  `ModelProfile` per capability (no hardcoded `claude-sonnet-4-6`).
- [ ] `[F07]` Every agent LLM call writes `ModelCall`+`CostLedgerEvent` **or logs a warning with
  the skip reason**; each wrapped in an OTEL span with non-null `agent_run_id`.
- [ ] `[F10]` Under `ALEPH_AUTH_MODE=oidc` the container-env credential fallback is disabled
  (raises); under `local` it still works.
- [ ] Unit: capability→model resolution; cost log-on-skip; env-cred gate raises under oidc.
  Integration: a prod-profile Live turn writes `model_calls` with the prod model + `agent_run_id`.

### Phase 4b — Retrieval quality hardening  `[F31, F32]`
- [ ] `[F31]` On an empty/irrelevant FTS result, the router does **not** tag arbitrary
  most-recent pages as `primary`; it returns "no confident match" (composer told the wiki lacks
  coverage) instead of confidently grounding on unrelated recent pages.
- [ ] `[F32]` The orchestrator cannot pass off a substantive answer from `search_wiki` summaries
  alone — substantive questions route through the `retriever` subagent/composer (enforced, not
  just prompt-suggested).
- [ ] Unit: router empty-FTS path returns the no-confident-match shape; a guard test for the
  summary-only answer path.

### Phase 5 — Re-embed worker + model-profile route + `set_model_profile`  `[F17, F18, F33]`
- [ ] `document_chunks.embedding_model` exists (migration) + set on embed.
- [ ] `[F17]` Switching a project's embedding binding enqueues `reembed_job` → re-embeds only
  stale chunks, updates the index, writes `embeddings.reembedded` + `ModelCall`/`CostLedgerEvent`.
- [ ] `[F18]` `PUT /v1/projects/{id}/model-profile {profile_name}` switches the profile (ledgered);
  the agent `set_model_profile` tool actually changes it (approval-gated).
- [ ] `alembic check` clean after the migration. Unit: stale-chunk selection; profile-switch
  ledger. Integration: switch → `reembed_job` enqueued → `embedding_model` updated.

### Phase 6 — A2UI deltas for Hypotheses  `[F11]`
- [ ] Hypotheses tab emits the bound `hypothesis_cards_v09` data model; a new/updated hypothesis
  arrives via an `updateDataModel` **delta** (in-place patch, no full refetch/reload).
- [ ] Unit: diff→`updateDataModel` round-trip. **Playwright** `hypotheses-delta.spec.ts`: create a
  hypothesis → card appears **without a reload** (assert no navigation, DOM node added).

### Phase 7 — Delete legacy pipeline + dormant-surface/dead-code cleanup + docs  `[F12, F13, F14, F15, F16, F19, F20, F28, F29, F30]`
- [ ] `[F16, F30]` `routes/assistant.py` turn path, `assistant_turn_job`, `AssistantTurnWorkflow`
  (incl. `budget_gate`, regex `query_rewrite`) **deleted**; no import remains; Live chat still works.
- [ ] `[F20]` `echo` subagent removed from prod. `[F12]` dead "view diff" actions
  (`DiffCard`/`ApprovalCard`) removed (no handler-less actions remain).
- [ ] `[F13]` Notes `NotebookCellCard` children are **rendered** by `NotesSurface` (or the backend
  stops building them) — no wasted backend work. `[F14]` ClaimCard reaches the wiki panel (panel
  stream includes the page's claims) **or** the unused builder is removed. `[F15]` wiki embeds are
  wired **or** removed — no always-empty dead path.
- [ ] `[F19]` `artifact_card` builder gains a real caller (Library Phase 9) **or** is removed;
  no catalog card is dead. `[F28]` Settings drops soft/hard cost-cap fields (single global cap only).
- [ ] `[F29]` Docs match reality: `implementation-log.md` (curator wave + 2026-06-17 batch + this
  remediation), honest `system-assessment.md` rewrite, `CLAUDE.md` corrections (Playwright render
  = not-yet until Phase 9; connector contract; rule #5/#7 language), AIQ `2.0.0→2.1.0`, fix the
  `server.ts` `catalogId` comment drift + add `unpin` to the frontend `ACTION_NAMES`.
- [ ] `grep -rn "assistant_turn_job\|AssistantTurnWorkflow\|budget_gate\|\"echo\"" packages apps`
  → only intended/no output. **Playwright** `live-chat-smoke.spec.ts` (chat still streams + a card)
  + `settings.spec.ts` (no soft/hard caps).

### Phase 8 — Connectors via custom AIQ image + AIQ robustness  `[F01, F02, F21, F25, F26, F34]` (*spec first*)
- [ ] `[F02]` A custom `aiq-agent` image registers arxiv/semantic_scholar/openalex/lens/rss/
  huggingface_hub as NAT data-source functions (reusing `ConnectorBase`); `ConnectorRegistry` wired.
- [ ] `[F01]` A project's enabled `ConnectorBinding`s scope a research run (submit-time
  `data_sources` filter over the registered set); arxiv-enabled project returns arxiv sources.
- [ ] `[F21]` AIQ-down at dispatch **requeues** (no permanently-`pending` stranded run).
  `[F25]` throttle `acquire` is genuinely idempotent for a held slot (no self-eviction).
  `[F26]` `_parse_report` tolerates a differing AIQ output shape (no silent zero-citation commit;
  logs + degrades explicitly). `[F34]` document the `dataset_rows` connector path (or implement one).
- [ ] Integration: enable arxiv → run shallow research → ≥1 captured Source from arxiv.
  **Playwright** `research-connectors.spec.ts`: enable a connector in UI → research via Live → a
  Source from that connector appears in the Library.

### Phase 9 — Raw-source visibility: Library + render worker + viewer cards + builder fixes  `[F27, +feature]` (*spec first*)
- [ ] "Artifacts" tab renamed **"Library"** with **Sources** (ingested) + **Artifacts** (built) sections.
- [ ] Real Playwright **render worker** captures JS/SPA pages (rendered DOM + screenshot stored as
  a `SourceAsset`); plain HTTP remains fallback. CLAUDE.md's "Playwright render" claim becomes true.
- [ ] Each raw source renderable in its own card: **PDF viewer**, **webpage viewer** (screenshot +
  text), **document viewer** — opening from Library renders the asset via the presigned-URL endpoint.
- [ ] `[F27]` Builder soft spots fixed: `_node_chart_freeze` no longer a no-op (or removed from the
  path); `source_pack` exporter produces a real manifest (no `sources=[]` placeholder).
- [ ] Integration: ingest PDF + JS-URL → raw `SourceAsset` bytes (+ render screenshot for URL);
  presigned-URL returns bytes. **Playwright** `library-viewer.spec.ts`: upload PDF → Library →
  Sources → click → PDF viewer renders; ingest URL → webpage viewer renders screenshot + text.

## 4. UI/UX checks (every UI-touching phase)
- [ ] No dead controls — every visible button/link/tab does something (handler-less actions removed).
- [ ] Live updates land without a full reload where promised (Hypotheses delta; wiki presence/pulse;
  merge banner refresh).
- [ ] Dark mode + system theme correct on any new/changed surface (cards on tokens).
- [ ] No console errors on exercised Playwright flows (assert `browser_console_messages`).
- [ ] New/renamed surfaces ("Library", merge banner, viewer cards) use existing card primitives +
  spacing — not a bare JSON `Column`.
- [ ] A11y smoke (axe-core on Library viewer + merge card): interactive cards keyboard-reachable,
  images have alt text.

## 5. Observability / integrity checks (every phase)
- [ ] Each new mutation has an `ActionLedgerEvent`; `…/ledger/verify` stays `ok` after the flows.
- [ ] Each new LLM/embed call has `ModelCall`+`CostLedgerEvent` (or a logged skip) + an OTEL span;
  `purpose` strings set.
- [ ] No `project_id`-less rows introduced (rule #6).

## 6. Out of scope (explicit)
- A full revision-diff **viewer** (the dead diff *actions* are removed, not implemented).
- Per-tab A2UI deltas beyond Hypotheses (sequenced, not stubbed).
- OIDC SSE auth (separate documented gap) — except the env-cred gate in Phase 4.

## 7. Execution protocol
- TDD per task; frequent commits on `audit-remediation`; one logical change per commit.
- Bring up the stack via `run-aleph` before integration/Playwright phases; keep it warm.
- Phase end: run **all** Global Gates (§2) + the phase's acceptance/validation + UI/UX (§4) +
  observability (§5); paste evidence; flip the boxes. Fix any regression before moving on.
- Honest reporting: if something can't pass (e.g. the AIQ image needs NGC creds not present), I
  stop, state the exact blocker, and propose options — never a faked green.
- New Playwright specs → `tests/playwright/specs/`; backend e2e → `tests/e2e/`. Keep
  `implementation-log.md` updated per phase.

## 8. Findings Coverage Matrix (every audit finding → phase)

| ID | Finding (2026-06-29 audit) | Tier | Phase |
|----|----|----|----|
| F01 | Per-project connectors never reach AIQ (research = Tavily-only) | 1 | 8 |
| F02 | `aleph-connectors` suite orphaned / `ConnectorRegistry` never populated | 1 | 8 |
| F03 | Curator `cross_link` (step 3) never built — siblings don't cross-link | 1 | 2 |
| F04 | Curator not enqueued at the `commit_revision` chokepoint | 1 | 2 |
| F05 | Page-merge proposals invisible end-to-end | 1 | 3 |
| F06 | Agent bypasses `ModelProfile` (hardcoded sonnet) — rule #7 | 2 | 4 |
| F07 | Agent cost best-effort/silently-dropped, no OTEL span — rule #5 | 2 | 4 |
| F08 | Ledger holes: alias upsert, repair_broken_links, handedit, feedback — rule #4 | 2 | 1 |
| F09 | Hash chain never verified at runtime | 2 | 1 |
| F10 | Credentials fall back to container env vars | 2 | 4 |
| F11 | A2UI data-delta substrate dormant | 3 | 6 |
| F12 | Diff "view diff" actions wired to a dead handler | 3 | 7 |
| F13 | Notes `NotebookCellCard` children built but discarded | 3 | 7 |
| F14 | ClaimCard never reaches the wiki panel | 3 | 7 |
| F15 | Wiki embeds path always empty | 3 | 7 |
| F16 | Legacy `assistant_turn` pipeline still shipped (+ `budget_gate`) | 3 | 7 |
| F17 | Phantom re-embed worker (`reembed_for_project` missing) | 3 | 5 |
| F18 | `set_model_profile` non-functional setter | 3 | 5 |
| F19 | 10/13 catalog cards LLM-whim only; `artifact_card` 0 callers | 3 | 7 (+9) |
| F20 | `echo` test subagent ships in prod | 3 | 7 |
| F21 | AIQ-down at dispatch silently strands the run | 4 | 8 |
| F22 | Overview identified by brittle `title == project.title` | 4 | 2 |
| F23 | `apply_merge` doesn't rewrite page-body markdown | 4 | 3 |
| F24 | Curator dedup/recurate silently disabled without a ModelProfile | 4 | 2 |
| F25 | Throttle re-acquire can evict a held slot | 4 | 8 |
| F26 | `_parse_report` assumes AIQ output shape (silent zero-citation) | 4 | 8 |
| F27 | Builder `_node_chart_freeze` no-op + `source_pack` placeholder manifest | 4 | 9 |
| F28 | Settings shows soft/hard cost-cap fields (vs single global cap) | 4 | 7 |
| F29 | Doc drift: impl-log/assessment/CLAUDE.md/AIQ-version/server.ts comment/`unpin` | 4 | 7 |
| F30 | `query_rewrite` regex stub (legacy path) | 4 | 7 (dies w/ F16) |
| F31 | Router tags arbitrary recent pages `primary` on empty FTS | 4 | 4b |
| F32 | Orchestrator can answer from `search_wiki` summaries alone | 4 | 4b |
| F33 | Re-embed `needs_reembed` reference with no implementation | 4 | 5 (w/ F17) |
| F34 | No connector emits `dataset_rows` | info | 8 |

All 34 findings are assigned. Additive feature work (Library/render/viewer cards) rides Phase 9.
