# EditorialReviewer

Scheduled + threshold-triggered LangGraph workflow with five
LLM-judged subagents:

| Subagent | Kind emitted | Default severity |
|---|---|---|
| `contradiction` | `contradiction` | medium |
| `weak_source` | `weak_source` | low |
| `narrative_gap` | `narrative_gap` | low |
| `coverage_gap` | `coverage_gap` | medium |
| `factual_freshness` | `factual_staleness` | low |

Each subagent receives a small project payload (titles + summaries +
body excerpts of up to 25 recent pages) and an
`extraction`-or-`synthesis` capability LLM call producing structured
findings.

Findings with `severity ≥ medium` automatically create a paired
`ApprovalRequest` so the analyst sees them in the BriefsSurface as
`ApprovalCard`s; lower-severity findings appear as `FindingCard`s
without forced approval.

## Triggers

- **scheduled** — operator cron (every 24h by default).
- **threshold** — wiki revision rate exceeds N revisions / hour
  (configurable per project; default 20).
- **manual** — `POST /v1/projects/{id}/reviews/runs` (owner; lands in
  Inc 8 with the eval suite).

## Deep Agents harness

The spec calls for the Deep Agents harness (planning + subagents +
HITL). Inc 5 ships the workflow shape with subagents-in-series via
LangGraph and a clear contract that swaps in the Deep-Agents harness
when its PyPI release stabilizes. Behaviorally equivalent — the
LangGraph version is auditable, instrumented, and ledgered.
