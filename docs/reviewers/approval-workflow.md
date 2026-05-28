# Approval workflow

`ApprovalRequest` wraps `ApprovalDecision` (Inc 3) with workflow
state and target context. One row per pending decision; one
`ApprovalDecision` row per resolution.

## Lifecycle

```
ReviewFinding (severity ≥ medium) → ApprovalRequest(status=pending)
                                  → BriefsSurface as ApprovalCard
                                  → analyst clicks Approve / Reject
                                  → POST /v1/projects/{id}/approval-requests/{id}/decide
                                  → ApprovalDecision row written
                                  → ApprovalRequest.status flipped
                                  → ReviewFinding.status mirrored
```

Other `target_kind`s supported:
- `synthesis_proposal` — Inc 3's synthesis flow, now also rendered
  here.
- `wiki_revision` — pending major revisions awaiting analyst review
  (rare; lands when needed).
- `hypothesis_update` — for hypothesis confidence transitions that
  require human sign-off (e.g. moving to `refuted`).

## API

- `GET /v1/projects/{id}/approval-requests?status=pending|approved|rejected`
- `POST /v1/projects/{id}/approval-requests/{request_id}/decide`
  with body `{decision, reason?}`. Owner-only.

The Inc 4 ActionRouter's `approve` / `reject` handlers route via the
synthesis proposal path for proposal targets; review findings route
through `decide()` in `aleph_reviewer.approval_service`.
