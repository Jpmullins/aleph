# Hypotheses

A `Hypothesis` is an analyst-authored structured question: title,
statement, current confidence, version history, evidence list. They're
first-class so reviewers, the assistant, and the Builder can reason
about *what the project is trying to learn*, not just the wiki text.

## Confidence states

| State | When |
|---|---|
| `under_investigation` | Initial, or no evidence. |
| `weakly_supported` | `net_support` (positive - negative weights) ≥ 1 |
| `well_supported` | `net_support` ≥ 3 and ≥ one ≥1.5-weight supporting evidence |
| `contested` | `net_support` ≤ -1 |
| `refuted` | `net_support` ≤ -3 |
| `abandoned` | Manually set; status remains `active` but the analyst marks it as not worth pursuing. |

`aleph_hypotheses.confidence.next_confidence_from_evidence` is the
single source of truth for transitions. Evidence stance ∈ `supports`,
`contradicts`, `contextualizes` (last one carries weight 0 for state).

## Versions

Every confidence transition writes a new `HypothesisVersion` with the
prior version as `parent_version_id`. Versions are immutable
(Postgres triggers on `hypothesis_versions`).

## Evidence

`HypothesisEvidence` rows link a hypothesis to one of:
`claim` | `source_page` | `chunk` | `finding` | `other_hypothesis`.
Weight defaults to 1.0; analyst can override. Each evidence add
re-derives confidence and triggers a version if state changed.

## API

```
POST   /v1/projects/{id}/hypotheses              # owner/editor
GET    /v1/projects/{id}/hypotheses
GET    /v1/projects/{id}/hypotheses/{id}
PATCH  /v1/projects/{id}/hypotheses/{id}         # update statement; records version
POST   /v1/projects/{id}/hypotheses/{id}/evidence
GET    /v1/projects/{id}/hypotheses/{id}/versions
```

## Wiki integration

Hypotheses can be referenced in wiki body markdown as
`[[Hypothesis:H0001]]`. The wiki agent's alias normalization treats
the short_id as a canonical alias. Inc 7's Builder includes the
hypothesis section in exported artifacts when the project's template
opts in.
