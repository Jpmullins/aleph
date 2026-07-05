# Audit Remediation (Batch 1: Curator · Rules · Dead-code) — Design

**Status:** approved (brainstorm 2026-06-30) · **Increment:** post-Inc-8 wave
**Owner:** Justin Mullins · **Author:** Claude

This is **spec 1 of 5** in the audit-remediation program (see §0). It covers the
three corrective workstreams — **WS-B curator completeness**, **WS-C rule
integrity**, **WS-D dead-code & drift**. The two larger, additive workstreams get
their own specs, sequenced after this one:

- **Spec 4 — Connectors via a custom AIQ image (WS-A, Option B).** A custom
  `aiq-agent` image registering Aleph's connectors (arxiv, semantic_scholar,
  openalex, lens, rss, huggingface_hub) as NAT plugins reusing the existing
  `ConnectorBase` implementations + per-project `data_sources` scoping.
- **Spec 5 — Raw-source visibility (WS-E).** Playwright render worker, the
  "Library" tab (ingested sources + built artifacts, sectioned), document /
  PDF / webpage viewer cards.

## 0. Program context — why this batch first

A six-subsystem audit (2026-06-29) found the core research loop genuinely works,
but surfaced ~20 findings across four tiers. The user chose to land the
correctness/cleanup fixes **before** the additive features. Specs 4–5 build new
surface area on top of a hardened spine. Each spec is production-complete for its
scope — no `v1`/stub, no "enhance later" (per the repo's no-versioning rule).

This batch deliberately bundles WS-B/C/D because they are tightly interrelated
corrective work over the same files (wiki services, the assistant agent, the
worker registry) and share one migration + one test pass.

## 1. Goals / non-goals

**Goals**
- The wiki graph knits on **every** authoring path (not just synthesis/ingest),
  and siblings cross-link from prose, not only from pre-existing `[[markup]]`.
- A user can discover and act on a proposed page merge without hand-calling REST.
- Every state mutation writes an `ActionLedgerEvent` in-transaction (rule #4 has
  zero known holes); the hash chain is verifiable at runtime.
- The conversational surface resolves its model through `ModelProfile` (rule #7)
  and records cost reliably with an OTEL span (rule #5).
- No dead parallel chat path; no phantom/undelivered workers; docs match reality.

**Non-goals**
- Connector/AIQ image work (spec 4) and raw-source UI (spec 5).
- Re-architecting retrieval, the orchestrator topology, or the A2UI catalog
  beyond wiring the one dormant delta path for Hypotheses.
- A full revision-diff viewer (the dead "view diff" actions are *removed* here,
  not implemented; a diff viewer, if wanted, is a separate item).

---

## 2. WS-B — Curator completeness

### 2.1 Chokepoint enqueue (fixes: bootstrap/notes-promote never knit)

Today `curate_page_job` is enqueued only from `aiq_synthesis.py:221` and
`wiki_ingest.py:242` (the latter fragilely nested inside the mechanical-review
`if pending is None: continue` guard). Bootstrap-only projects and the
notes→wiki promote path (`notes.py:267`) never knit.

**Approach.** `aleph-wiki` is a leaf-ish package and must not import arq/redis.
Add an injected hook:

- `WikiService.__init__(..., enqueue_curate: Callable[[UUID, UUID], Awaitable[None]] | None = None)`.
- `commit_revision(...)`, after a successful commit, if `origin != "curator"` and
  the hook is set, `await self._enqueue_curate(project_id, page.id)` — best-effort
  (failure logged, never fails the commit).
- The arq-aware callers (api + workers) construct `WikiService` with a hook that
  closes over the arq pool: `lambda pid, pgid: pool.enqueue_job("curate_page_job", str(pid), str(pgid))`.
- Remove the ad-hoc enqueues in `aiq_synthesis.py` / `wiki_ingest.py`; the hook
  now covers them uniformly. The ingest enqueue is thereby **decoupled** from the
  review-dispatch loop.
- Callers that legitimately should knit: bootstrap overview + seed pages
  (`bootstrap.py:198`), notes-promote (`notes.py:267`), synthesis
  (`synthesis_workflow.py:239`), ingest (`agent/workflow.py:592,641`).

**Loop guard** unchanged: curator commits pass `origin="curator"` and don't
enqueue. Covered by an existing test; extend it to assert the hook is *not*
called for `origin="curator"` and *is* called for the others.

### 2.2 Build `cross_link` (deterministic step 3 — fixes problem B)

The curator spec's step 3 was never implemented; `CuratorService.curate()` runs
only `_register_aliases` + `_repair_links`. `repair_broken_links` only re-resolves
*existing* `[[markup]]` whose `dst_page_id` is null — it never creates links from
prose. So a page that mentions a sibling by name without `[[ ]]` never links.

**Approach.** New `CuratorService._cross_link(project_id, page_id)`, run inside
`curate()` after repair, deterministic and idempotent:
1. Load the project's title+alias set (one query; reuse the alias machinery).
2. Scan the committed page body for occurrences of those surface forms that are
   **not already** inside a wikilink (regex-guard against existing `[[...]]`
   spans and code fences). Bounded to the one new page's text.
3. For each match → resolve to the target page, inject `[[Title]]` markup at the
   first unlinked occurrence, and (because the body changed) commit via
   `commit_revision(origin="curator")` so the new `WikiLink` rows are written
   through the normal machinery (which also ledgers).
4. Idempotent: re-running finds the occurrence already wrapped, no-ops.

Guardrails: only link the **first** occurrence per target per page (avoid link
spam); respect hand-edited sections (`respect_hand_edits=True`); skip
self-links; cap at a configurable `curator_cross_link_max_targets` (default 20,
logged when hit — no silent cap).

### 2.3 Surface merge proposals (fixes: slice 2c invisible)

`PageMergeProposal`s are created and the approve/`apply_merge` route exists, but
there's no UI and the conversational tool doesn't mention them.

**Approach (reuse existing surfaces, no new tab):**
- **Wiki surface banner.** The "needs-attention" review banner
  (`WikiSurface`/`surfaces.py` wiki messages) gains a *pending merges* section:
  each pending `PageMergeProposal` renders an ApprovalCard (source → target,
  rationale, similarity) wired to the existing approve/reject routes via the
  action-router. Reuses the W6 ApprovalCard + action pattern — no new component.
- **Conversational.** `wiki_curation_status` (`copilot_agent.py:262`) also reports
  the pending-merge count + titles. Add an approve/reject merge **action** through
  the action-router (approval-gated like other consequential ops) so the agent
  can surface and the user can resolve a merge in chat.

### 2.4 WS-B tests
- Unit: `_cross_link` adds a missing sibling link + idempotency; chokepoint hook
  fires for bootstrap/notes/synthesis/ingest origins and **not** for curator.
- Integration: seed overview with a broken `[[Topic]]`, promote a note that
  mentions an existing sibling in prose → assert curate ran, link repaired,
  sibling cross-linked, ledger counts, and a pending merge (if dup) renders on
  the wiki surface payload.

---

## 3. WS-C — Rule integrity

### 3.1 Close the ledger holes (rule #4)

Four wiki-side mutations write no `ActionLedgerEvent`:
`AliasService.upsert`, `AliasService.repair_broken_links`,
`HandeditService.mark_section`/`clear_section`, `feedback_service.write_feedback`.

**Approach.** Thread `LedgerWriter` into each service (constructor injection,
matching the `WikiService`/`hypothesis_service` pattern) and append in the same
transaction, before/after the mutation flush as appropriate:

| Mutation | `action_kind` | payload highlights |
|---|---|---|
| `AliasService.upsert` | `wiki.alias.upsert` | alias, dst_page_id |
| `AliasService.repair_broken_links` | `wiki.links.repair` | count + repaired `WikiLink` ids + (src,dst) pairs |
| `HandeditService.mark_section` | `wiki.handedit.mark` | page_id, anchor |
| `HandeditService.clear_section` | `wiki.handedit.clear` | page_id, anchor |
| `feedback_service.write_feedback` | `wiki.feedback.write` | page_id, verdict |

`repair_broken_links` is the priority — it's the curator's main action and
currently rewrites link targets with no audit trail. Because hand-edits now
ledger, they also become live-pushable (closes the "hand-edits write no ledger
event → not pushed live" honest-limit noted in the assessment).

No new table; existing `action_ledger_events` + chain head. No migration.

### 3.2 Runtime chain verification

The sha256 chain is computed on append but never verified at runtime (only DB
immutability triggers protect it; `_compute_chain_hash` is exercised only in a
unit test).

**Approach.** Add `GET /v1/projects/{id}/ledger/verify` (EDITOR-gated): walk the
project's events in order, recompute each `chain_hash`, return
`{ok: bool, count: int, first_divergence: {event_id, expected, actual} | null}`.
Add a tamper-detection test (mutate a payload in a test DB → verify reports the
divergence). Cheap, on-demand; no scheduled job.

### 3.3 Agent honors `ModelProfile` (rule #7)

Orchestrator + all subagents hardcode `_AGENT_MODEL = "claude-sonnet-4-6"`
(`copilot_agent.py:876`), bypassing capability→model resolution, so
`aleph-production` (Opus) never applies to the conversational surface.

**Approach.** `_gateway_chat_model` (`copilot_agent.py:898`) resolves the model
from the project's `ModelProfile` via a `Capability`, replacing the constant:

- Orchestrator → `Capability.synthesis`.
- Subagent → role-appropriate capability: retriever→`synthesis`,
  researcher→`synthesis`, wiki_builder→`synthesis`, viz_builder→`code`,
  analyst→`synthesis`, reviewer→`judge`.

The profile is loaded once per turn (it's already available where the agent is
built). Cost pricing (`copilot_cost_callback`) reads the resolved model id, so it
stays accurate. Falls back to the dev profile's binding if no project profile.

### 3.4 Robust agent cost + OTEL span (rule #5)

`AgentCostCallbackHandler` silently skips on unresolved scope / missing usage /
write error, writes `agent_run_id=None`, `latency_ms=0`, and opens no span.

**Approach.**
- Wrap each agent LLM call in an OTEL/Langfuse span (`assistant.turn` /
  `assistant.subagent.<name>`) via the callback's `on_llm_start`/`on_llm_end`
  (start/stop the span; record `latency_ms` from the span).
- Thread the `agent_run_id` (the orchestrator already mints an `AgentRun`) into
  the handler so cost rows attribute to the run.
- Keep best-effort writes (never crash the turn) but **log a warning on every
  skip** with the reason (no silent drop). Handle `CancelledError` by flushing
  any pending usage on `on_llm_error`/teardown so a disconnect bills partial
  rather than nothing.

This makes rule #5 *honestly* "every agent LLM call records cost or logs why it
couldn't" — and CLAUDE.md is updated to state that precisely (§5).

### 3.5 Gate the env-credential fallback

`ConnectorCredentialService` falls back to container env vars
(`aiq_internal.py:96-102`) for 6 connector kinds unconditionally, violating
"credentials never from container env."

**Approach.** Gate the fallback on `ALEPH_AUTH_MODE == "local"` (the dev-only
mode). Under `oidc`, the env fallback is disabled and a missing
`ConnectorCredential` raises (no silent env leak). Logged when the fallback is
used. (Spec 4 revisits credential delivery for the custom-image connectors; this
just stops the rule violation now.)

---

## 4. WS-D — Dead-code & drift

### 4.1 Delete the legacy `assistant_turn` chat pipeline

Post-W6, Live is the sole chat surface, but `assistant.router` is still mounted
(`main.py:95`), `post_message` still enqueues `assistant_turn_job`
(`assistant.py:354`) → `AssistantTurnWorkflow` (with a `budget_gate` node that
hard-blocks on spend — contradicting the no-cost-gating decision).

**Approach.** Remove: the router include, `routes/assistant.py`'s turn-submission
path, `assistant_turn_job` + its arq registration, `AssistantTurnWorkflow` and
its nodes (`budget_gate`, the regex `query_rewrite` stub), and their tests.
**Verify first** (grep) that no Live-path code imports shared helpers from these
modules; relocate any genuinely shared helper (e.g. the retrieval router is in
`aleph-assistant/retrieval`, untouched) before deleting. The
`WikiFirstRetrievalRouter` and its tests stay.

### 4.2 Implement the re-embed worker (drift repair)

`reembed_for_project` / `needs_reembed` are referenced (`embedding.py:8-10`,
`retrieval.py:104-110`) but don't exist; changing the embedding model leaves
stale-model vectors forever.

**Approach.**
- Record the embedder model id per chunk. If `DocumentChunk` lacks an
  `embedding_model` column, **add one** (nullable, backfilled to the current
  default) — **new Alembic migration**.
- Implement `aleph_rks.retrieval.reembed_for_project(session, project_id, …)`:
  find chunks whose `embedding_model` ≠ the project's current `embedding`
  capability binding, re-embed via `LiteLLMClient.embed` (writes
  ModelCall+CostLedgerEvent), update vectors + `RetrievalIndexRecord`, set
  `embedding_model`, ledger `embeddings.reembedded`.
- `reembed_job` arq worker (registered in `arq.py`/`jobs/__init__.py`), enqueued
  when a project's embedding binding changes (hook in the model-profile switch
  route, §4.3) and exposed as a manual `POST …/reembed`.
- Bounded + idempotent (only stale chunks); top-level batching; logs counts.

### 4.3 Make `set_model_profile` functional

The agent tool admits it can't switch profiles.

**Approach.** Add a ledgered `PUT /v1/projects/{id}/model-profile {profile_name}`
route that sets the project's active named profile (`aleph-dev` / `aleph-production`),
writes `project.model_profile.set`, and (if the embedding binding changed)
enqueues `reembed_job` (§4.2). Rewire the agent `set_model_profile` tool to call
it through the **approval-gated** action-router (consequential). Rename is not
needed — the tool now actually sets.

### 4.4 Wire A2UI deltas for the Hypotheses tab

The `diff_data_model`/`updateDataModel` substrate + `hypothesis_cards_v09`
bound-card builder exist but no route emits a bound data model; tabs refresh via
polling + the `changes` stream.

**Approach.** The Hypotheses surface route (`surfaces.py` `_hypotheses_messages`)
emits the **bound** `hypothesis_cards_v09` data model instead of the plain
interactive surface; `SurfaceStreamer` emits `updateDataModel` deltas on change;
the frontend `HypothesesSurface` renders the bound cards and patches props in
place (no full refetch). Remove the now-superseded no-op `["surface"]`
invalidations for this tab. This exercises the delta path end-to-end (its
designed use); other tabs keep polling for now (explicitly sequenced, not
stubbed).

### 4.5 Cleanup + doc refresh

- **Settings:** remove the soft/hard cost-cap fields (`Drawers.tsx:82-85`); keep
  the single global cap (the agreed model).
- **`echo` subagent:** remove from the production subagent set (`copilot_agent.py:1012`).
- **Dead "view diff" actions:** remove the `open` actions in `DiffCard.tsx` /
  `ApprovalCard.tsx:68` that hit a non-existent handler (and the unused
  `DiffCard` if nothing references it after). No diff viewer is added here.
- **Docs:**
  - `docs/implementation-log.md` — append the **curator wave** (slices 1/2b/2c)
    and the unlogged **2026-06-17 batch**; note this remediation.
  - `docs/system-assessment.md` — honest rewrite: the audit findings + which are
    now fixed by this batch; drop the over-optimistic "all 8 rules hold / rule #5
    closed" framing.
  - `CLAUDE.md` — correct the **Playwright render** claim (it doesn't exist yet;
    spec 5 builds it), the **connector contract** (the `ConnectorBase` suite is
    not the live path until spec 4), and the rule **#5/#7** language to match
    §3.3–3.4.
  - AIQ version strings `2.0.0 → 2.1.0` (`client.py:4`, `aiq-config-default.yml:9`).

---

## 5. Data model & migrations

- **No new tables.** New `action_kind` strings only (no schema).
- **One migration:** add `document_chunks.embedding_model` (nullable String,
  backfilled to the current default embedder id) for drift detection (§4.2).
  File: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_chunk_embedding_model.py`.
  `alembic check` must be clean after.

## 6. Error handling / safety

- Curate enqueue is best-effort (never fails a commit); `_cross_link` failure
  degrades to repair-only (already-committed page intact).
- Ledger appends are in the mutation's transaction — a ledger failure rolls back
  the mutation (correct: no unaudited mutation).
- Re-embed is bounded, idempotent, resumable; a failure leaves old vectors.
- Agent cost stays best-effort but now logs every skip.
- Deleting the legacy pipeline is guarded by a grep-for-importers check first.

## 7. Testing & evals

- **Unit (`pytest -m "not integration"`):** `_cross_link` + idempotency;
  chokepoint hook origin matrix; each newly-ledgered mutation asserts a ledger
  row + `action_kind`; chain-verify tamper detection; agent capability→model
  resolution (asserts non-default model under `aleph-production`); cost-callback
  logs-on-skip; `reembed_for_project` selects only stale chunks.
- **Integration (`pytest -m integration`):** note-promote → curate knit +
  cross-link + ledger counts; merge proposal renders on the wiki surface payload
  + approve `apply_merge`; `GET …/ledger/verify` returns ok on a clean project;
  model-profile switch → reembed_job enqueued; Hypotheses `updateDataModel` delta
  round-trip.
- **Browser (per the repo's per-wave rule):** wiki merge ApprovalCard
  approve/reject; Hypotheses in-place delta update; Settings no longer shows
  soft/hard caps.
- **Gates:** `ruff`, `ruff format`, `pyright`, `alembic check`, web `tsc`/ESLint,
  evals `--gate strict` all green before merge.

## 8. Components / files (touch list)

- `packages/aleph-wiki/src/aleph_wiki/wiki_service.py` — `enqueue_curate` hook.
- `packages/aleph-wiki/src/aleph_wiki/curator_service.py` — `_cross_link`.
- `packages/aleph-wiki/src/aleph_wiki/{alias_service,handedit_service,feedback_service}.py`
  — ledger writes.
- `apps/workers/.../jobs/{bootstrap,aiq_synthesis,wiki_ingest}.py`,
  `apps/api/.../routes/notes.py` — provide the curate hook; drop ad-hoc enqueues.
- `apps/api/.../routes/{ledger.py (new verify), model_profile.py (new), reembed}` ;
  `surfaces.py` (merge banner + hypotheses bound model); `copilot_agent.py`
  (ModelProfile resolution, set_model_profile rewire, remove echo,
  curation_status merges); `copilot_cost_callback.py` (span + run id + log-on-skip);
  `routes/aiq_internal.py` + `credentials.py` (gate env fallback).
- `apps/api/.../routes/assistant.py`, `packages/aleph-assistant/.../agent/*`,
  `apps/workers/.../jobs/` — **delete** legacy turn pipeline.
- `packages/aleph-rks/src/aleph_rks/{embedding,retrieval}.py` +
  `apps/workers/.../jobs/reembed.py` (new) — re-embed worker.
- `apps/web/src/a2ui/components/*` (HypothesesSurface bound cards, remove dead
  diff actions), `Drawers.tsx` (cost-cap fields).
- Alembic migration (§5). Docs (§4.5).

## 9. Sequencing (within this spec — each step ships complete, merges independently)

1. **Ledger holes + chain verify (WS-C.1–.2)** — smallest, highest-integrity win;
   no UI.
2. **Curator chokepoint + `cross_link` (WS-B.1–.2)** — graph knitting; depends on
   the ledgered `repair_broken_links` from step 1.
3. **Merge-proposal surface (WS-B.3).**
4. **Agent ModelProfile + cost + env-cred gate (WS-C.3–.5).**
5. **Re-embed worker + model-profile route + `set_model_profile` (WS-D.2–.3)** —
   the one migration lands here.
6. **A2UI Hypotheses deltas (WS-D.4).**
7. **Delete legacy pipeline + cleanup + doc refresh (WS-D.1, .5).**

Each step is production-complete for its scope (no stub/`v1`).
