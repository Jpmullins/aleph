# Increment 8 — Eval Suite + UserFeedback + Regression Gates

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md`
**Depends on:** Inc 0–7
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 8.1 Scope

Increment 8 unifies and operationalizes everything previous increments shipped as scattered evals. The `EvalDataset` / `EvalCase` / `EvalRun` / `EvalResult` models land as full first-class objects (Inc 0 carried a skeleton `aleph-evals.runner`; this increment makes it real). `UserFeedback` inline affordances ship in every relevant A2UI surface. The fixture project corpus is unified. AIQ public benchmarks (FreshQA, DeepResearch Bench) are wired in. CI enforces regression gates: any wiki-coverage drop, citation-broken rate spike, permission leakage, or cost drift > 15% blocks merge.

After Inc 8, the product is self-monitoring. Every increment from Inc 0 forward has had per-increment evals; Inc 8 makes the **cross-cutting** suite the source of truth and fail-fast on regression.

### In scope

- `EvalDataset` / `EvalCase` / `EvalRun` / `EvalResult` models (replacing Inc 0 skeleton)
- `UserFeedback` model + inline affordances on `ClaimCard`, `SourceCard`, `ChartCard`, `FindingCard`, assistant messages
- Fixture project corpus (canonical seed for eval runs)
- Eval runner CLI + Python API + Arq scheduled job
- AIQ benchmark adapter (FreshQA + DeepResearch Bench wrapping AIQ's existing harness)
- Aleph-specific eval datasets (consolidated from Inc 1–7) + new Inc 8 datasets
- Cost regression detector
- CI gates: permission leakage, wiki coverage, citation correctness, cost drift, A2UI schema validity
- Per-`ModelProfile` baselines + gates
- UserFeedback → EvalCase pipeline (high-signal feedback becomes regression tests)

### Out of scope

This is the final increment.

### Dependencies

- Inc 0–7 fully
- All per-increment eval datasets land in `packages/aleph-evals/datasets/inc<N>_<area>/`; Inc 8 unifies discovery + execution

### Downstream

None — this completes the Aleph build. Beyond Inc 8: connector additions, A2UI catalog version bumps, ModelProfile expansions, future increments per top-level §16.2.

---

## 8.2 Repository changes

```
packages/
└── aleph-evals/                        # expanded from Inc 0 skeleton
    └── src/aleph_evals/
        ├── __init__.py
        ├── models.py                   # EvalDataset, EvalCase, EvalRun, EvalResult
        ├── runner.py                   # main entry; runs datasets + writes reports
        ├── scorers/
        │   ├── __init__.py
        │   ├── retrieval.py            # page-selection recall / descent recall
        │   ├── citation.py             # broken-citation rate, citation correctness
        │   ├── coverage.py             # wiki coverage given source corpus
        │   ├── permission.py           # permission leakage (must be 0)
        │   ├── synthesis.py            # synthesis_flag precision/recall
        │   ├── hypothesis.py           # confidence rule correctness
        │   ├── artifact.py             # lineage completeness, reproducibility
        │   ├── a2ui.py                 # schema validity over recorded surface payloads
        │   ├── cost.py                 # cost drift vs baseline
        │   └── llm_judge.py            # judge-LLM scorer wrapper for editorial dims
        ├── adapters/
        │   ├── __init__.py
        │   ├── freshqa.py              # AIQ FreshQA harness wrapper
        │   └── deepresearch_bench.py   # AIQ DeepResearch Bench wrapper
        ├── fixtures/
        │   ├── corpus/                 # the canonical fixture sources
        │   ├── seed_project.py         # rebuilds a fixture project from corpus
        │   └── reference_data/         # ground-truth expectations
        ├── ci/
        │   ├── gate.py                 # exit-code logic + report formatting
        │   └── baselines.json          # baseline metrics per profile
        └── cli.py                      # `aleph-eval <args>`

packages/aleph-core/src/aleph_core/
└── feedback.py                         # UserFeedback model

apps/api/src/aleph_api/routes/
├── evals.py                            # eval runs, reports, datasets
└── feedback.py                         # extends Inc 1; adds UserFeedback CRUD

apps/web/src/
├── components/
│   ├── FeedbackButton.tsx              # 👎 / 🚩 / "mark wrong" affordances
│   └── FeedbackComposer.tsx            # modal with structured fields
└── a2ui/components/
    ├── ClaimCard.tsx                   # add inline feedback button
    ├── SourceCard.tsx                  # add inline feedback button
    ├── ChartCard.tsx                   # add "misleading" flag
    └── FindingCard.tsx                 # add "false-positive" flag

.github/workflows/
├── eval.yml                            # run on every PR + main
└── eval-nightly.yml                    # full bench (slow datasets)
```

---

## 8.3 Domain model

```python
# packages/aleph-evals/src/aleph_evals/models.py

class EvalDataset(CommonColumns, Base):
    __tablename__ = "eval_datasets"
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # e.g. "aleph-coverage", "aleph-page-selection", "freshqa"
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # retrieval | citation | coverage | permission | synthesis | hypothesis |
    # artifact | a2ui | cost | freshqa | deepresearch
    case_count: Mapped[int] = mapped_column(nullable=False, default=0)
    fixture_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # filesystem path under packages/aleph-evals/datasets/<inc>_<area>/<name>.jsonl
    gate_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # blocking | warning | metric_only
    gate_thresholds_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # per-profile: {"aleph-dev": {...}, "aleph-production": {...}}
    introduced_in_increment: Mapped[int] = mapped_column(nullable=False)

class EvalCase(Base):
    """One example. Materialized rows for query speed; source of truth is fixture file."""
    __tablename__ = "eval_cases"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    eval_dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    case_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expected_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tags_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="fixture")
    # fixture | user_feedback | regression_capture
    origin_ref_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # for user_feedback origin: UserFeedback.id

    __table_args__ = (UniqueConstraint("eval_dataset_id", "case_key"),)

class EvalRun(CommonColumns, Base):
    __tablename__ = "eval_runs"
    eval_dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    model_profile_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # aleph-dev | aleph-production | custom
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # null for cross-project eval runs (the typical case)
    runner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # git sha of the eval package at run time
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    # running | passed | failed | error
    pass_count: Mapped[int] = mapped_column(nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(nullable=False, default=0)
    metrics_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=0)
    report_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # s3://bucket/evals/{run_id}/report.html

class EvalResult(Base):
    __tablename__ = "eval_results"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    eval_run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    eval_case_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(nullable=False)
    score: Mapped[float | None] = mapped_column(nullable=True)
    # for scored-not-binary scorers
    actual_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    diff_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

# packages/aleph-core/src/aleph_core/feedback.py

class UserFeedback(CommonColumns, Base):
    __tablename__ = "user_feedback"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # claim | source | chart | finding | hypothesis | assistant_message | wiki_page
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    signal: Mapped[str] = mapped_column(String(32), nullable=False)
    # wrong | misleading | low_quality | low_confidence | irrelevant | excellent
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    promoted_to_eval_case_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # set when an owner promotes this feedback into a regression test case
```

Migration `<timestamp>_inc8_evals_feedback.py` creates these tables. Inc 0's `eval_datasets`/`eval_cases`/etc. skeleton (if any rows existed) is migrated forward; new columns added.

---

## 8.4 Eval runner

```python
# packages/aleph-evals/src/aleph_evals/runner.py

@dataclass
class RunOptions:
    datasets: list[str] | Literal["all"]
    profile: str = "aleph-dev"
    project_id: UUID | None = None
    fixture_corpus: str = "default"      # name of fixture corpus to use
    cost_cap_usd: Decimal = Decimal("10")
    fail_fast: bool = False              # CI uses true for sanity, false for full reports
    parallelism: int = 4
    write_results_to_db: bool = True

async def run(options: RunOptions) -> EvalRunReport:
    """1. Discover datasets from packages/aleph-evals/datasets/**/*.jsonl
       2. For each enabled dataset: rebuild the fixture project; for each case,
          run the corresponding scorer; persist EvalResult.
       3. Aggregate metrics: per-dataset, per-profile, overall.
       4. Compare against baselines (ci/baselines.json) per profile.
       5. Emit HTML report to s3://bucket/evals/{run_id}/report.html.
       6. Return EvalRunReport with pass/fail per dataset + gate verdict.
    """
```

### Discovery convention

`packages/aleph-evals/datasets/inc<N>_<area>/<dataset_name>.jsonl` — JSONL where each line is an `EvalCase` payload conforming to the dataset's scorer contract. Discovery is filesystem-walk; new datasets land by adding files (no code changes needed).

Each dataset directory carries a `dataset.toml` with:

```toml
name = "aleph-coverage"
kind = "coverage"
gate_kind = "blocking"        # blocking | warning | metric_only
description = "..."
introduced_in_increment = 1
scorer = "coverage"

[gate_thresholds.aleph-dev]
recall_min = 0.85
precision_min = 0.80

[gate_thresholds.aleph-production]
recall_min = 0.90
precision_min = 0.85
```

### CLI

```bash
aleph-eval run --profile aleph-dev --datasets all
aleph-eval run --profile aleph-production --datasets aleph-coverage,aleph-citations
aleph-eval baseline --profile aleph-production --datasets all     # update baselines
aleph-eval promote-feedback <user_feedback_id>                     # turn UserFeedback into EvalCase
aleph-eval report --eval-run-id <id>                               # render HTML
```

### Cost cap

The runner accumulates costs from the underlying LLM/tool calls. If `cost_cap_usd` would be exceeded, run halts with `status="error"`, partial results recorded, ledger event `eval_run.cost_capped`.

---

## 8.5 Scorers (one per dataset kind)

Each scorer is a Python function with signature `async def score(case, project_context) -> CaseResult`. Returns:

- `passed: bool`
- `score: float | None` (for scored datasets)
- `actual_jsonb` (the produced output)
- `diff_jsonb` (structured diff vs expected)
- `latency_ms`, `cost_usd`

### Scorer list (consolidated from Inc 1–7 + new for Inc 8)

| Dataset | Scorer | Source increment | Gate |
|---|---|---|---|
| `aleph-coverage` | `coverage` | Inc 1 | blocking |
| `aleph-alias-extraction` | `coverage` | Inc 1 | warning |
| `aleph-page-selection` | `retrieval` | Inc 2 | blocking |
| `aleph-descent-correctness` | `retrieval` | Inc 2 | blocking |
| `aleph-citation-correctness` | `citation` | Inc 2 | blocking |
| `aleph-synthesis-flag-precision` | `synthesis` | Inc 2 | warning |
| `aleph-synthesis-coverage` | `coverage` | Inc 3 | blocking |
| `aleph-citation-verification-recall` | `citation` | Inc 3 | blocking (100%) |
| `aleph-connector-routing` | `synthesis` | Inc 3 | warning |
| `aleph-surface-payload-validity` | `a2ui` | Inc 4 | blocking |
| `aleph-action-routing` | `a2ui` | Inc 4 | blocking |
| `aleph-mechanical-citation-recall` | `citation` | Inc 5 | blocking |
| `aleph-mechanical-broken-link-recall` | `citation` | Inc 5 | blocking |
| `aleph-editorial-contradiction-recall` | `llm_judge` | Inc 5 | warning |
| `aleph-editorial-coverage-precision` | `llm_judge` | Inc 5 | warning |
| `aleph-hypothesis-confidence-correctness` | `hypothesis` | Inc 5 | blocking |
| `aleph-chart-spec-validity` | `a2ui` | Inc 6 | blocking |
| `aleph-dataset-diff-correctness` | `coverage` | Inc 6 | blocking |
| `aleph-cell-edit-versioning` | `coverage` | Inc 6 | blocking |
| `aleph-bibliography-correctness` | `artifact` | Inc 7 | blocking (100%) |
| `aleph-lineage-completeness` | `artifact` | Inc 7 | blocking |
| `aleph-render-pixel-diff` | `artifact` | Inc 7 | warning |
| **`aleph-permission-leakage`** | `permission` | Inc 8 (cross-cutting) | blocking (0 leaks) |
| **`aleph-cost-drift`** | `cost` | Inc 8 | warning (15% drift) |
| **`aleph-feedback-regression`** | varies | Inc 8 | blocking |
| **`freshqa-aleph`** | adapter | Inc 8 | warning |
| **`deepresearch-bench-aleph`** | adapter | Inc 8 | warning |

### `permission` scorer

Probes random principal/project pairs. Verifies every cross-project read returns 404 (not 200 with empty data, not 403). Zero leaks is the only passing outcome.

### `cost` scorer

For each scored dataset, captures per-case token + cost figures. Compares against `baselines.json` per profile. Drift > 15% (configurable) → warning. Drift > 50% → blocking.

### `feedback_regression` scorer

For each promoted UserFeedback that became an EvalCase, re-runs the original flow and verifies the previously-bad behavior no longer occurs.

---

## 8.6 AIQ benchmark adapters

### FreshQA

`packages/aleph-evals/src/aleph_evals/adapters/freshqa.py`:

- Imports AIQ's bundled FreshQA dataset
- For each question, runs Aleph's assistant chat flow (Inc 2) end-to-end (not just AIQ's bare research)
- Scores answer correctness + freshness via judge model
- Reports tracked separately as `freshqa-aleph` dataset

This validates the Aleph-wrapped flow performs at least as well as AIQ's raw FreshQA bench — measuring the cost of the wiki-first abstraction.

### DeepResearch Bench

`packages/aleph-evals/src/aleph_evals/adapters/deepresearch_bench.py`:

- Wraps AIQ's existing DeepResearch Bench harness
- Runs end-to-end: prompt → `/synthesize` → approved wiki → assistant answer
- Reports separately as `deepresearch-bench-aleph`

Both adapters run **nightly** (not per-PR) via `.github/workflows/eval-nightly.yml`. Per-PR runs use the Aleph-specific suite only — fast enough to gate merges.

---

## 8.7 Fixture corpus

`packages/aleph-evals/fixtures/corpus/` contains the canonical seed sources (PDFs, MDs, HTMLs) used to rebuild a known fixture project for evals. Sources are license-clean for redistribution (Wikipedia snapshots, arXiv preprints, public domain reports). Total size ≤ 200 MB.

`seed_project.py` programmatically:
1. Creates a fresh Project named `eval-fixture-{run_id}`
2. Uploads all corpus sources
3. Waits for normalization + chunking + wiki ingest
4. Verifies the fixture project's wiki state matches a stored expected snapshot (`fixtures/reference_data/wiki_baseline.json`)
5. Returns the project_id for downstream eval cases

The fixture project is **deleted** after the eval run (or kept for owner-flagged debug runs).

---

## 8.8 UserFeedback affordances

### Affordances per A2UI card

- `ClaimCard` — 👎 button. Modal: signal (`wrong | low_confidence | irrelevant`), rationale (required for `wrong`), severity.
- `SourceCard` — 🚩 button. Signal options include `wrong | misleading | low_quality | irrelevant`.
- `ChartCard` — "Flag as misleading" affordance. Required rationale.
- `FindingCard` — "False positive" affordance (for reviewer findings the analyst disagrees with).
- `HypothesisCard` — "Refute" or "Endorse" lightweight signals.
- `AssistantMessage` (chat) — 👎 with optional rationale. Per-message.
- `WikiPage` — "Report problem" affordance in the page header.

All affordances POST to `/v1/projects/{id}/feedback`.

### Promotion to EvalCase

`aleph-eval promote-feedback <user_feedback_id>` (also accessible via API endpoint `POST /v1/feedback/{id}/promote`):

- Owner-only
- Captures the surrounding context (the query + retrieval result + composer output + cited claims) as an `EvalCase` payload
- Sets `EvalCase.origin="user_feedback"` and `EvalCase.origin_ref_id=user_feedback.id`
- Adds to `aleph-feedback-regression` dataset
- The same flow is then re-run in CI on every PR; if the original bad behavior recurs, regression test fails

---

## 8.9 CI gates

`.github/workflows/eval.yml` runs on every PR + main:

```yaml
- name: Eval (aleph-dev)
  run: aleph-eval run --profile aleph-dev --datasets all --fail-fast

- name: Eval (aleph-production)
  run: aleph-eval run --profile aleph-production --datasets all

- name: Gate
  run: aleph-eval gate --eval-run-id $RUN_ID
  # Exit 0 if all blocking datasets pass under both profiles; else non-zero.
```

### Gate logic

For each dataset:
- `gate_kind=blocking` and dataset failed under the profile being checked → CI fails
- `gate_kind=warning` and dataset failed → warning printed; PR comment posted; CI does NOT fail
- `gate_kind=metric_only` → recorded, never blocks

Per-profile gating:
- Failures under `aleph-dev` block dev work (PRs)
- Failures under `aleph-production` block deployment (the deploy workflow runs the same eval against the prod profile)

`.github/workflows/eval-nightly.yml` runs the slow benchmarks (FreshQA, DeepResearch Bench) nightly against `main`, posts a report to a designated dashboard. Drift > some threshold opens an issue automatically.

### Baselines

`packages/aleph-evals/ci/baselines.json` carries the metric baseline per dataset per profile. Updated explicitly via `aleph-eval baseline --profile <p> --datasets all` after a deliberate quality bump (PR-reviewed change).

Cost baselines update less frequently — the goal is to detect drift, not chase the moving target. Cost baseline rotation is a manual quarterly review (project policy).

---

## 8.10 HTTP API

All under `/v1/`.

### Evals

- `GET /eval-datasets` — list with kind, gate_kind, current pass rate
- `GET /eval-datasets/{name}` — detail
- `GET /eval-runs` — list; filter by profile, dataset, status
- `GET /eval-runs/{id}` — detail with results summary
- `GET /eval-runs/{id}/results?case_filter=failed` — per-case results
- `GET /eval-runs/{id}/report` — signed URL to HTML report
- `POST /eval-runs` — owner; dispatches an eval run; body `{datasets, profile, project_id?}`

### Feedback

- `POST /projects/{id}/feedback` — body `{target_kind, target_id, signal, rationale?, severity?}`
- `GET /projects/{id}/feedback` — owner/editor; list
- `POST /feedback/{id}/promote` — owner; promotes UserFeedback to EvalCase in `aleph-feedback-regression`
- `DELETE /feedback/{id}` — owner; soft delete (ledgered)

---

## 8.11 Tests

### Unit

- `aleph-evals/tests/test_runner.py` — discovery walks the datasets dir; dataset.toml parsed; cases loaded
- `aleph-evals/tests/test_scorers_*.py` — one per scorer; fixture cases → expected pass/fail
- `aleph-evals/tests/test_gate.py` — gate logic: blocking failures → exit nonzero; warning → exit zero with comment
- `aleph-evals/tests/test_baselines.py` — cost drift calc; threshold logic
- `aleph-evals/tests/test_freshqa_adapter.py` — mock AIQ FreshQA harness → expected metric shape
- `aleph-evals/tests/test_promotion.py` — UserFeedback → EvalCase round-trip; subsequent runner picks it up

### Integration (`tests/e2e/`)

- `test_full_eval_run_inc8.py` — Boot fixture corpus → run all blocking datasets under `aleph-dev` → all pass → metrics written to DB + HTML report uploaded
- `test_regression_blocks_merge.py` — Introduce a code change that breaks page selection → run eval → `aleph-page-selection` fails → gate command exits nonzero
- `test_user_feedback_promotion_e2e.py` — Submit feedback → promote → new EvalCase in `aleph-feedback-regression` → run eval → that case present in results
- `test_permission_leakage_eval.py` — The `aleph-permission-leakage` dataset's scorer probes random pairs; gate requires zero leaks
- `test_cost_drift_alarm.py` — Force per-case cost up by 20% → cost drift dataset warns; PR comment posted (test the comment generator)
- `test_nightly_freshqa_runs.py` — Manual trigger of nightly workflow → FreshQA harness runs → results stored as separate EvalRun
- `test_eval_cost_cap.py` — Set tiny cost_cap_usd → eval halts cleanly with `status="error"`, partial results, ledger event

### Self-eval

`aleph-eval-meta` — an eval suite that checks the eval suite itself (scorers behave correctly on synthetic cases). Runs in the same CI workflow.

---

## 8.12 Documentation

- `docs/evals/overview.md` — what the suite covers, dataset taxonomy, gate kinds
- `docs/evals/datasets/<name>.md` — one doc per Aleph-specific dataset
- `docs/evals/adapters/freshqa.md`
- `docs/evals/adapters/deepresearch-bench.md`
- `docs/evals/baselines-and-drift.md`
- `docs/evals/user-feedback-promotion.md`
- `docs/operations/ci-gates.md`
- `docs/ui/feedback-affordances.md`
- `docs/implementation-log.md` — Inc 8 entry

---

## 8.13 Acceptance criteria

1. **Eval models real.** `EvalDataset`/`EvalCase`/`EvalRun`/`EvalResult` rows populated from runs.
2. **Discovery works.** Dropping a new `dataset.toml`+`*.jsonl` in `packages/aleph-evals/datasets/` makes the runner pick it up without code changes.
3. **Cross-profile gates.** CI runs all blocking datasets under both `aleph-dev` and `aleph-production`; failures block merge.
4. **Permission leakage = 0.** The `aleph-permission-leakage` scorer finds zero leaks under both profiles.
5. **UserFeedback affordances live.** Every relevant card has the feedback button; submitting writes `UserFeedback`; promotion to EvalCase works.
6. **Feedback regressions enforced.** Promoted feedback becomes a CI gate; the original bad behavior is caught if it recurs.
7. **AIQ benchmark adapters run nightly.** FreshQA + DeepResearch Bench produce results stored in EvalRun records.
8. **Cost regression alarms.** A 20% drift in per-case cost triggers a warning gate; PR comment posted.
9. **Reports.** HTML reports uploaded to S3; ArtifactsSurface owner view links to recent eval reports.
10. **Baselines management.** `aleph-eval baseline` updates `baselines.json` for the run profile; lineage preserved (baselines are PR-reviewed).
11. **Permission leakage on eval data.** Eval reports are project-scoped where they ran on project data; cross-project access returns 404.
12. **Docs complete.**
13. **No placeholders.**
14. **Implementation log written.**

---

## 8.14 Build complete

After Inc 8 lands:

- Every increment from Inc 0 onward is in final production form
- All evals run automatically on PRs
- All cost is tracked; all state changes are ledgered; all agents are sandboxed; all LLM calls go through one gateway
- The wiki is the primary KB; the assistant queries it; synthesis grows it; reviewers maintain it; the analyst owns approvals and rejection feedback
- Artifacts export with full lineage; user feedback closes the loop to evals
- A2UI renders the entire right panel + chat-inline cards; one declarative substrate, one catalog, zero LLM-generated code execution

The product can take on real OSINT and academic research work. Beyond Inc 8, ongoing work is:

- Adding new connectors as needed (each per top-level §16.2)
- Catalog v1.1, v1.2 component bumps as A2UI ecosystem matures
- New `ModelProfile` capabilities and gateway-served models
- The out-of-scope items in top-level §16.1 (multi-project shared knowledge, real-time co-editing, cross-project search) remain explicitly out of scope until a fresh design decision is made

There is no Increment 9. There are no v1/v2 deferrals. There is just Aleph.
