# Wiki Curator Backend — Design

**Status:** approved (brainstorm 2026-06-25) · **Increment:** post-Inc-8 wave
**Owner:** Justin Mullins · **Author:** Claude

## 1. Problem

The Aleph wiki is meant to grow as an interlinked LLM-wiki graph: a project's
overview page links to topic pages, topic pages cross-link each other and the
overview, and the whole thing is curated as research lands. Today it doesn't.

Observed (project "AI Distillation", 2026-06-24): the overview page had 3
wikilinks to 3 **existing, approved** topic pages, all rendered as broken
(non-clickable) because their `WikiLink.dst_page_id` was `NULL`. The targets
existed with exact-matching titles; nothing had ever resolved the links.

Root cause — wikilinks resolve only at **write time**
(`AliasService.resolve` → exact `WikiPage.title` match or alias). The
project-wide back-resolver `AliasService.repair_broken_links(project_id)`
exists but is called from only two places: the **ingest agent**
(`aleph_wiki/agent/workflow.py`) and a **manual button**
(`POST /v1/projects/{id}/wiki/aliases/repair-links`). The two paths that
actually build the wiki out — **bootstrap** (`apps/workers/.../jobs/bootstrap.py`,
seeds the overview with deliberately-broken `dst_page_id=None` links) and
**research/synthesis** (`aleph_wiki/synthesis_workflow.py`, resolves only the
new page's *own* outgoing links) — never repair anything. So:

- **(A)** Overview → topic links stay broken after research creates the topic.
- **(B)** Sibling topic pages never cross-link (each only resolves its own
  outgoing links at write time, before siblings exist).
- **(C)** No agent ever re-curates the overview/main page after research:
  no fold-in of new findings, no dedup of near-duplicate pages, no
  contradiction surfacing, no alias creation, no restructuring.

(C) is the missing **curator** in builder → researcher → curator.

## 2. Goals / non-goals

**Goals**
- After any page is committed (research, ingest, bootstrap, hand-edit), the
  graph knits automatically: broken links repair, siblings cross-link, the
  overview folds the new topic in.
- A real maintenance pass: near-duplicate detection + merge, contradiction
  surfacing, alias creation, incremental overview restructuring.
- Incremental, per page: the wiki connects as it grows ("feels live").
- Auto-apply curator edits (revisionable, ledger-tracked, optionally through
  the existing reviewers); only **destructive** ops (page merge/delete) require
  human approval via an ApprovalCard.

**Non-goals**
- Re-architecting how research is triggered or how pages are authored (the
  builder/researcher stay as-is; we add the curator and the trigger hook).
- Batch/scheduled curation. Trigger is per-commit (§5).
- Changing the retrieval path or the A2UI surfaces beyond the merge ApprovalCard.

## 3. Architecture (Approach B: dedicated curate job)

A new **`CuratorService`** in `aleph-wiki`, wrapped by an arq
**`curate_page_job`** in `aleph-workers`. The job is enqueued from the single
chokepoint every authoring path funnels through —
`WikiService.commit_revision` — on every successful **non-curator** commit.

```
commit (bootstrap | synthesis | ingest agent | hand-edit)
   └─ WikiService.commit_revision(...)            # existing
        ├─ writes revision + ActionLedgerEvent     # existing
        └─ if origin != "curator": enqueue curate_page(project_id, page_id)   # NEW hook
                                   │
                         curate_page_job (arq)      # NEW
                                   │
                         CuratorService.curate(project_id, page_id)  # NEW
                            1. repair_links        (deterministic)
                            2. register_aliases    (deterministic)
                            3. cross_link          (deterministic)
                            4. dedup_detect        (retrieval + LLM judge) → MergeProposal?
                            5. recurate_overview   (LLM, incremental edit)
```

**Loop guard (invariant):** curator-originated commits carry
`origin="curator"` (a new field on the commit call / revision metadata) and do
**not** re-enqueue `curate_page`. Without this, every curator overview edit
would trigger another curation pass forever. Enforced at the enqueue hook and
covered by a test.

**Debounce:** `curate_page` coalesces per `(project_id)` within a short window
(default 10s, config) using a Redis key, so a research wave landing N pages in
quick succession doesn't run N overlapping overview rewrites. Each page still
gets its deterministic knitting (1–3); the LLM steps (4–5) run on the coalesced
trailing invocation with the full current page set. This keeps "incremental per
page" for the visible graph while bounding LLM cost.

## 4. The pipeline steps

All mutations go through existing `WikiService`/`AliasService` machinery, so
each writes its `ActionLedgerEvent` in-transaction and respects
`respect_hand_edits=True`. LLM steps go through `LiteLLMClient` with a
`Capability` and `purpose="wiki.curate.<step>"`, producing `ModelCall` +
`CostLedgerEvent`. Each step is wrapped in an OTEL span `wiki.curate.<step>`
and emits an agent-event phase for live progress.

1. **repair_links** *(deterministic, always)* —
   `AliasService.repair_broken_links(project_id)`. Back-resolves every null
   `dst_page_id` project-wide. Idempotent (only touches nulls). Fixes (A).
2. **register_aliases** *(deterministic, always)* — ensure the new page's
   canonical title is registered, plus any obvious surface variants already
   referenced by other pages' unresolved links whose normalized form matches
   this page. Lets future links resolve through title drift. Idempotent.
3. **cross_link** *(deterministic, always)* — scan the new page's body for
   occurrences of existing page titles/aliases that are not already wikilinked;
   add `WikiLink` rows (resolved) and, where the body is regenerated, inject the
   `[[...]]` markup via a curator commit. Connects siblings → fixes (B). Bounded
   to the new page's text; matches against the project's alias/title set.
4. **dedup_detect** *(cheap retrieval + LLM judge)* — find top-K candidate
   pages similar to the new page (reuse `wiki_index` / embedding similarity,
   K=5, cosine ≥ threshold config). For each strong candidate an LLM judge
   (`Capability.judge`, `purpose="wiki.curate.dedup"`) answers "same concept?".
   On yes → create a **`PageMergeProposal`** and surface an ApprovalCard
   (§6). Never auto-merges.
5. **recurate_overview** *(LLM, incremental)* — load the project overview page;
   produce an **incremental edit** (`Capability.synthesis`,
   `purpose="wiki.curate.overview"`) that: ensures a `[[link]]` to the new page,
   adds/updates a 1–2 sentence summary of it in the appropriate section, and
   notes any contradiction between the new page's claims and existing overview
   claims. Commit via `WikiService.commit_revision(origin="curator")` so it goes
   through reviewers and is auto-applied as a new revision. Diff-style, not a
   full rewrite; respects hand-edits. If no overview page exists yet (pre-seed),
   skip.

Steps 1–3 are cheap and run on every page. Steps 4–5 run on the coalesced
invocation. Step failures degrade gracefully (§7).

## 5. Trigger & enqueue

- `WikiService.commit_revision` gains an `origin: str = "agent"` parameter
  (values: `agent`, `bootstrap`, `synthesis`, `ingest`, `hand_edit`,
  `curator`). After a successful commit, if `origin != "curator"`, it enqueues
  `curate_page` (via the arq Redis pool already used by the workers).
- Callers pass their `origin`. The curator passes `curator`.
- The enqueue is best-effort: a failure to enqueue is logged but does not fail
  the commit (curation is eventual, never on the write path).

## 6. Data model & approval

- **Curation run tracking:** reuse `AgentRun` with `agent_kind="curator"` +
  the existing agent-event phases (`repair_links`, `cross_link`, `dedup`,
  `recurate_overview`) for live progress in the Activity card. No new run table.
- **`PageMergeProposal`** (new table, `aleph-wiki`): `id, project_id,
  source_page_id, target_page_id, rationale, similarity, status
  (pending|approved|rejected), created_at/by, access_scope, ledger_event_id,
  trace_id, decided_at, decided_by`. Carries `project_id` + standard columns
  (rule #6). A new Alembic migration adds it.
- **ApprovalCard:** on a pending `PageMergeProposal`, the curator emits an
  A2UI ApprovalCard (reuse the W6 ApprovalCard + action-router pattern used for
  other gated actions). Approve → `CuratorService.apply_merge` (redirect
  source→target: re-point inbound links, alias the source title to the target,
  soft-delete/redirect the source page, all in one ledgered transaction).
  Reject → mark rejected, leave both pages. Merge is the only human-gated op.
- All other curator edits auto-apply through `commit_revision` (auto-reviewers,
  no human gate) per the autonomy decision.

## 7. Idempotency, safety, error handling

- **Idempotent steps:** repair_links touches only nulls; register_aliases and
  cross_link only add what's missing; recurate_overview is diff-based and
  hand-edit-respecting. Re-running `curate_page` for the same page is safe.
- **Loop guard:** §3 — curator commits don't re-enqueue.
- **Curation never breaks authoring:** it runs in a separate arq job; a failure
  leaves the committed page intact. arq retries with backoff; terminal failure
  emits an agent-event `curation_failed` and logs, page still exists.
- **Graceful LLM degradation:** if step 4 or 5's LLM call fails, the
  deterministic knitting (1–3) has already applied; the job records the partial
  outcome and retries the LLM steps only.
- **Bounded work:** dedup top-K, cross_link scans only the new page, debounce
  coalesces waves. No project-wide LLM rescans.
- **Config flags** (settings): `curator_enabled` (default true),
  `curator_debounce_seconds` (10), `curator_dedup_top_k` (5),
  `curator_dedup_min_similarity` (0.82). No silent caps — the dedup K and
  threshold are logged per run.

## 8. Testing & evals

- **Unit** (`pytest -m "not integration"`): each deterministic step against
  fixtures — repair_links resolves a null link to an exact-title page;
  cross_link adds a missing sibling link and is idempotent; register_aliases;
  the loop-guard (a `curator`-origin commit does not enqueue); dedup judge
  prompt assembly; the overview incremental-edit prompt assembly.
- **Integration** (`pytest -m integration`, compose stack): the exact
  regression — seed an overview with a broken `[[Topic]]` link, commit a
  `Topic` page via `commit_revision(origin="synthesis")`, assert (a) the curate
  job ran, (b) the overview link is now resolved, (c) sibling cross-links
  exist, (d) the expected `ActionLedgerEvent` count per mutation, (e) a
  `wiki.curate.*` ModelCall+CostLedgerEvent pair for the LLM steps.
- **Merge path:** dedup produces a `PageMergeProposal` + ApprovalCard; approve
  → `apply_merge` redirects links and soft-deletes the source with ledger rows.
- **Eval** (`aleph-evals`): a graph-connectivity scorer (fraction of wikilinks
  resolved; overview links to all real topic pages) + overview-coherence judge,
  added to the gate set.

## 9. Components / files

- `packages/aleph-wiki/src/aleph_wiki/curator_service.py` — `CuratorService`
  (steps 1–5, `apply_merge`).
- `packages/aleph-wiki/src/aleph_wiki/models.py` — `PageMergeProposal`.
- `packages/aleph-wiki/src/aleph_wiki/wiki_service.py` — `origin` param +
  enqueue hook on `commit_revision`.
- `apps/workers/src/aleph_workers/jobs/curate.py` — `curate_page_job` +
  registration in the worker settings; debounce via Redis.
- `apps/workers/.../jobs/bootstrap.py`, `aleph_wiki/synthesis_workflow.py`,
  `aleph_wiki/agent/workflow.py` — pass `origin=...` to `commit_revision`
  (synthesis/ingest can drop their now-redundant inline repair if the curator
  subsumes it — verify before removing).
- `apps/api/.../routes/` — ApprovalCard action route for merge approve/reject
  (reuse existing action-router).
- `apps/web/src/a2ui/components/` — ApprovalCard already exists; wire the merge
  proposal payload.
- Alembic migration for `page_merge_proposals`.
- Tests under each package's `tests/`; eval scorer in `aleph-evals`.

## 10. Sequencing (within this increment, no stubs)

1. **Knitting core** (complete, shippable): `origin` param + enqueue hook +
   loop guard; `CuratorService` steps 1–3; `curate_page_job` + debounce;
   unit + integration tests. This alone permanently fixes (A) and (B).
2. **Curation layer**: steps 4–5 (dedup judge + incremental overview recurate),
   `PageMergeProposal` + migration + ApprovalCard + `apply_merge`; tests.
3. **Evals**: graph-connectivity + overview-coherence scorers into the gate.

Each step is production-complete for its scope (no `v1`/stub) and merges
independently.
