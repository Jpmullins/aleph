# Regression suite

The full eval suite runs against both `aleph-dev` and `aleph-production`
profiles on every PR.

## What it catches

- **Permission leakage.** Always zero. Any non-empty `leaked_targets`
  list anywhere → block.
- **Citation drift.** Broken `[cN]` markers above the project's
  threshold (per profile) → block.
- **Wiki coverage drop.** `coverage` datasets below the per-profile
  `min_pass_rate` → block.
- **Cost drift.** Phase-level cost > 15% off the baseline (per
  profile) → warn (not block; cost regressions are noisy).
- **A2UI schema validity.** Recorded surface payloads must validate
  against the catalog version they declare; a renderer breaking change
  surfaces immediately.

## Baselines

`packages/aleph-evals/src/aleph_evals/ci/baselines.json` carries the
per-profile expected metrics. Update via PR alongside the code change
that moves the metric.

## Failure-mode catalog

| Symptom | Likely cause | Fix |
|---|---|---|
| `permission` dataset fails | new route forgot project-scope dep | grep for `ProjectScopeDep` use; add 404-on-non-membership check |
| `citation` precision drops | wiki_service committed claims with synthetic markers | re-run mechanical reviewer; check `citation_verification` node passed |
| `coverage` drops | wiki agent concept_extraction prompt regressed | inspect Langfuse trace; check `concept_extraction.md` |
| `cost` warn | AIQ phase changed pricing or added more LLM calls per turn | tokenomics adapter shows per-phase deltas |
| `synthesis_flag_precision` drops | retrieval router's coverage_judgment logic changed | retrieval router unit tests must cover the failing scenario |

## Promoting a user-feedback case

When a `marked_wrong` feedback row turns into an `EvalCase`, it
shows up in the per-project `user_feedback:{project_id}` dataset. To
fold it into the cross-cutting suite, copy the case JSONL line into
the right `inc*_*/` fixture with a `tags: [...]` annotation; CI
exercises both copies until the regression is fixed.
