# Eval suite

Full cross-cutting eval suite. Runs in CI on every PR + main; the
nightly job (`.github/workflows/eval-nightly.yml`) runs the slow
public benchmarks (FreshQA, DeepResearch Bench) against the vendored
AIQ harness.

## Discovery

Datasets live at
`packages/aleph-evals/datasets/<inc>_<area>/manifest.yaml` with one
manifest per increment. Each manifest lists datasets with:

```yaml
datasets:
  - name: aleph-permissions
    file: aleph_permissions.jsonl
    kind: permission        # picks the scorer
    gate_kind: blocking     # blocking | warning | metric_only
    gate_thresholds:
      default: { min_pass_rate: 1.0 }
      aleph-production: { min_pass_rate: 1.0 }
    introduced_in_increment: 8
```

The runner walks every `inc*_*/` directory, loads the manifest, then
discovers the JSONL case files.

## Scorers

| `kind` | What it scores |
|---|---|
| `retrieval` | top-K recall of expected pages |
| `citation` | broken-marker count + precision against expected markers |
| `coverage` | wiki coverage of expected concepts |
| `permission` | leaked_targets count must be zero |
| `synthesis` | coverage_judgment matches expected |
| `cost` | actual cost within drift_pct of baseline per profile |
| `metric_only` | always passes; surfaces metrics only |

## Gate kinds

- **blocking** — pass_rate < threshold → CI exit 1.
- **warning** — never blocks; logs a warning in the report.
- **metric_only** — no gate; metric surfaces in dashboards.

`permission` datasets are always treated as blocking regardless of
the manifest setting (defense in depth).

## CI gate

`aleph_evals.ci.gate.write_summary(report, path)` writes a structured
JSON report and returns the exit code. The GitHub Action wires this
through `python -m aleph_evals --gate strict --profile aleph-dev` and
`--profile aleph-production` so both baselines are exercised.

## UserFeedback → EvalCase pipeline

`POST /v1/projects/{id}/feedback` with
`signal ∈ {marked_wrong, misleading, false_positive}` promotes the
feedback row into an `EvalCase` under a per-project dataset
`user_feedback:{project_id}`. The dataset is `gate_kind=warning` by
default; once enough cases accumulate the project operator can flip
the kind to `blocking` for a project-specific regression suite.

## Adding a dataset

1. Drop `your_dataset.jsonl` + extend `manifest.yaml` under
   `packages/aleph-evals/datasets/inc<N>_<area>/`.
2. Pick the right `kind` (or write a new scorer in
   `aleph_evals.scorers` and register it).
3. Set `gate_kind` + per-profile `gate_thresholds`.
4. PR with the new dataset triggers CI evaluation.
