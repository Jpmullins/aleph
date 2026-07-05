# WP-3 — Native research loop; AIQ deleted

**Date:** 2026-07-03 · **Package:** WP-3 of `GOAL.md` · **Proves:** F2 (loop half + deletion)
**Status:** implementing

## Problem

Research runs in a separate 4-GB NVIDIA AIQ container reached over HTTP, with credentials,
sources, cost records, and events round-tripping through an `/internal/v1/aiq/*` callback
surface, a redis slot throttle, a deferred-submit job, and a 20-second poll job — the
poll/slot-leak failure class lives entirely in this plumbing. Meanwhile the typed connector
suite (`ConnectorBase` implementations for tavily/openalex/arxiv/…) sits fully written but
never registered, because AIQ ran its own `data_source_registry` instead. The wiki-side
`SynthesisWorkflow` is healthy and stays; only the way a research report gets produced changes.

## Design

### 1. `aleph-research` package + `deep_research_job`

New package `packages/aleph-research` (deps: `aleph-core`, `aleph-db`, `aleph-models`,
`aleph-connectors`, `aleph-scholar`, `aleph-rks`, `aleph-wiki`, `langgraph`). One workflow,
`ResearchWorkflow`, run by a new arq job `deep_research_job(ctx, agent_run_id, token)` —
in-process, no HTTP callbacks, no polling, no slots.

LangGraph graph (ContextVar-based ctx like `SynthesisWorkflow`, every node `@with_phase`
so Activity streams progress automatically):

```
plan → search → ingest → reflect ─(loop ≤ max_iterations, plateau cutoff)→ search
                              └──→ compose → synthesize → END
```

- **plan** (LLM, `Capability.SYNTHESIS`, purpose `research.plan`): topic → 3–6 subqueries,
  each tagged with preferred tool kinds. JSON response format.
- **search**: fan the subqueries across the **bound** tools — the project's enabled
  connectors (via the registry, below) plus `ScholarService.search_openalex` /
  `expand_citations`. Pure HTTP, no LLM. Results deduped by URL/DOI.
- **ingest**: LLM relevance triage (purpose `research.triage`, `Capability.CLASSIFICATION`)
  selects ≤ `max_sources_per_iter`; each selected result is fetched via its connector's
  `fetch()` and registered with `register_uploaded_source` — `connector_kind` = the real
  connector kind, `source_metadata_jsonb` ⊇ `{doi, openalex_id, doi_verdict}` when the
  scholar path verified it (verification via `verify_dois` for any result carrying a DOI;
  `ok=False` results are dropped, never ingested). Normalize jobs enqueue as usual.
- **reflect** (LLM, purpose `research.reflect`): given accumulated source summaries, decide
  `gaps: [...]` → new subqueries, or `done`. **Bounds:** `max_iterations` (deep=3,
  shallow=1); **plateau cutoff:** if an iteration ingests 0 new sources, stop regardless.
- **compose** (LLM, purpose `research.compose`): write `body_md` citing sources as `[cN]`
  markers (the prompt lists each ingested source's `short_id`/title/url as `c1..cN`);
  `aleph_scholar.style_pass` (LLM-free) normalizes the draft; build a **`ResearchReport`**
  — the renamed `AIQReport` dataclass (`topic, body_md, summary, sources,
  citations_by_marker, claims`), interface unchanged.
- **synthesize**: hand the report to the **unchanged** `SynthesisWorkflow`
  (`aiq_report` state key renamed `report`); enqueue `curate_page_job` per committed page.

**Failure semantics (no strands):** the job wraps the whole graph in try/except — any
exception marks the `AgentRun` `failed` with `error_text`, `completed_at`, and a
`phase_failed` event (the `builder_job` model). No deferred-submit, no poll timeout, no slot
release: the failure classes structurally disappear. `agent_kind` = `"deep_research"` |
`"shallow_research"` (ActivityCard `KIND_LABELS` re-mapped).

### 2. Tool binding + allowlist enforcement (F2: scoping by *binding*)

- Worker startup registers the typed connectors into `get_registry()` (they are complete;
  today nothing registers them). Registration list: `tavily, openalex, arxiv,
  semantic_scholar, exa, serper, rss, lens` (the document-output research set).
- At job start, `_bound_tools(project_id)` resolves `Connector ⋈ ConnectorBinding` where
  enabled (explicit binding beats `enabled_by_default`), resolves each connector's
  credential via `ConnectorCredentialService.decrypt_for_callback` **in-process** (the
  logic re-homed from `aiq_internal.get_credential`, incl. the local-mode env fallback),
  and instantiates ONLY those connectors. **A disallowed connector is never constructed,
  never bound into the graph** — enforced by a unit test with a registry spy, and visible
  in agent-events (the search node emits a `research.tools` payload listing bound kinds).
- Consensus is **not** bound into the worker loop (it is the Live researcher subagent's
  quota-metered screening tool; the loop's discovery needs are OpenAlex/web). Stated here
  so the omission is a decision, not drift.

### 3. Re-targets

- `POST /v1/projects/{id}/synthesize` keeps its contract (EDITOR, `{topic, depth}` →
  `{agent_run_id}`) but now: creates the `AgentRun(agent_kind=depth_kind, status=pending)`,
  writes ledger `synthesize.dispatch`, enqueues `deep_research_job`. `aiq_job_id` leaves the
  response model. Proposal list/approve/reject routes unchanged.
- `copilot_agent._start_research_impl` (self-calls `/synthesize`) — wording only.
- `bootstrap_project_job` per-topic fan-out enqueues the native job.
- `tests/e2e/test_bootstrap_job.py` re-targets its dispatch monkeypatch.
- `audit/checks/aiq-research-to-wiki.sh` → rewritten as `research-to-wiki.sh` (native).

### 4. AIQ deletion inventory

**Delete whole:** `packages/aleph-aiq/`; `apps/workers/.../jobs/{aiq_submit,aiq_synthesis}.py`;
`apps/api/.../routes/aiq_internal.py`; compose `aiq-server` service (image
`nvcr.io/nvidia/blueprint/aiq-agent:2.1.0`, `mem_limit: 4g`) + `aiq-data` volume;
`deploy/compose/{aiq-config-default.yml,aiq-init-jobs.sql,aiq-init-checkpoints.sql}`;
`tests/unit/test_aiq_{poll_release,submit_job,throttle}.py`, `test_dispatch_research.py`,
`test_settings_concurrency.py`.

**Surgical edits:** `arq.py` (job registry), `jobs/__init__.py`, `routes/__init__.py` +
`main.py` (router), api+workers `settings.py` (`aiq_base_url`, `aiq_max_concurrent_jobs`),
`middleware/auth.py` (`/internal/v1/aiq/` self-auth prefix), `synthesize.py` (dispatch),
`bootstrap.py`, `postgres-initdb.sh` (aiq_* DB loop + init mounts), compose env
(`AIQ_BASE_URL`, `AIQ_MAX_CONCURRENT_JOBS`), `.env.example` (`NGC_API_KEY`, `AIQ_BASE_URL`),
`bootstrap-local.sh` (nvcr login + `:8001` line), `synthesis_workflow.py` (`AIQReport` →
`ResearchReport`, `aiq_report` → `report`, docstrings), `citation_verification.py` +
`mechanical/workflow.py` + `aleph_connectors/{__init__,registry}.py` docstrings,
`aleph_core/schemas/ledger.py` `ActorKindStr` (drop `"aiq_agent"` — historical rows keep
the string; code stops writing it), `apps/web/src/lib/api.ts` + `ActivityCard.tsx`
(`aiq_deep|aiq_shallow` → native kinds), `aleph-evals` adapters, CI workflow if it
references AIQ.

**Keep:** migration `20260527_1900_inc3_aiq_synthesis.py` verbatim (rule 6: revisions are
immutable; it creates `connector_credentials`/`synthesis_proposals`/`approval_decisions`,
which are not AIQ infra). The aiq_* postgres databases stop being created on fresh volumes;
existing dev volumes may retain them harmlessly (documented, not migrated).

### 5. Settings / env

Workers settings add `research_max_iterations_deep=3`, `research_max_iterations_shallow=1`,
`research_max_sources_per_iter=6`, `research_max_total_sources=15`. Deleted keys per §4.

### 6. Security posture

- The loop runs in `aleph-workers` with the same posture as every other worker job: agent
  token minted at dispatch, typed service calls, no raw provider SDKs — all LLM calls via
  `LiteLLMClient.chat(...)` (gateway), every call auto-writing `ModelCall` +
  `CostLedgerEvent` with `purpose="research.*"` and the run's `agent_run_id` (rule 5).
- Credentials decrypt in-process and never leave the worker; the `/internal/v1/aiq/*`
  self-auth hole in the API middleware closes.
- Connector fetches download attacker-influenceable bytes (as AIQ did); they flow through
  the same `register_uploaded_source` → normalize pipeline (no new surface). Every ingest
  writes `source.create` + `source_version.create` ledger events (rule 4).

## Final State (falsifiable)

1. **Fresh-stack research round-trip.** `POST /synthesize {topic, depth:"deep"}` on the
   compose stack → `deep_research_job` runs → **≥3 Sources** with real `connector_kind`s +
   provenance metadata → draft synthesis page + **pending proposal in Briefs** → approve →
   `curate_page_job` runs. Verified live with DB queries + Activity phases shown.
2. **Cost attribution.** DB assertion: every `ModelCall` for the run's `agent_run_id` has a
   paired `CostLedgerEvent` and `purpose LIKE 'research.%'`; count > 0. Shown via psql.
3. **Binding enforcement.** Unit test: a disabled connector is never instantiated (registry
   spy) and the graph's bound-tool list excludes it; agent-events `research.tools` payload
   lists only enabled kinds. Live: disable a connector, run, grep events → zero calls.
4. **AIQ is gone.** `grep -ri aiq apps packages deploy scripts audit tests .github
   --exclude-dir=alembic` → **empty** (alembic versions are immutable history, GOAL rule 6;
   `docs/` waits for WP-7). `grep -rn nvcr.io` (same scope) → empty. `NGC_API_KEY` absent
   from `.env.example` and compose. `pnpm`-side: no `aiq_` strings in `apps/web/src`.
5. **mem_limit −4g.** Compose long-running sum drops 13.5g → 9.5g (aiq-server deleted);
   shown by summing the file's `mem_limit`s.
6. **No strands.** *(Amended 2026-07-03 to state arq semantics precisely.)* Three
   interruption paths, all converging to a terminal `failed`:
   (a) any in-graph exception → caught → `failed` + `error_text` + `completed_at`
   (`test_exception_marks_run_failed`, `test_missing_topic_fails_cleanly`);
   (b) `asyncio.CancelledError` from arq's `job_timeout` (600s) or a worker shutdown →
   marked `failed` best-effort, then re-raised (`test_cancelled_marks_failed_then_reraises`);
   (c) a hard worker kill leaves the run `running`; arq re-enqueues (default
   `retry_jobs=True`) and the job's **retry-guard** (`status != "pending"` at entry) marks it
   `failed` without re-running the graph — so a re-enqueue can never duplicate ingested
   sources or strand the run (`test_retry_after_interruption_does_not_rerun`). The run is
   enqueued only **after** its row is committed (dispatch commits before `enqueue_job`), so
   the worker never sees a missing run. An already-terminal run re-delivered by arq is an
   idempotent no-op (a `succeeded` run is never flipped to `failed`,
   `test_already_succeeded_retry_is_noop`). If `enqueue_job` itself fails after the run
   committed (redis down), dispatch marks the committed run `failed` before re-raising, so it
   never strands as `pending` (`test_dispatch_research_enqueue_failure_marks_run_failed`). The
   deferred-submit + poll + throttle failure classes no longer exist in code. Residual,
   arq-inherent and acceptable: while every worker is down, a killed run stays `running` until
   any worker processes the retry and marks it failed.
7. **Gates.** Full gate suite green; pyright warnings ≤ baseline; the deleted tests'
   coverage replaced by native-loop unit tests (plan/reflect JSON parsing, plateau cutoff,
   binding, report building, failure marking).
