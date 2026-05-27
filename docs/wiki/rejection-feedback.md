# Rejection feedback

When the analyst rejects a wiki draft, they record a reason. The reason
flows into the next compile of the same concept so the agent doesn't
repeat the mistake.

## Mechanism

`RejectionFeedback(concept_name, reason, rejected_revision_id?,
addressed_in_revision_id?)`. One row per rejection. `concept_name` is
the lookup key — for source pages it's `Source:<short_id>`; for topic
pages it's the canonical name.

On every compile of a given concept:

1. `feedback_service.pending_for_concept(project_id, concept_name)`
   returns rows where `addressed_in_revision_id IS NULL`.
2. The reasons are appended to the compile prompt as constraints.
3. After `wiki_service.commit_revision` succeeds,
   `feedback_service.mark_addressed(feedback_ids, revision_id)` updates
   the feedback rows.

## Auto-block

If five rejections accumulate for a concept without an approval in
between, the concept auto-blocks. The wiki agent skips it until the
analyst re-enables. (Inc 1 records the rows; Inc 5 wires the auto-block
behavior alongside the approval workflow.)

## API (Inc 1)

- `POST /v1/projects/{id}/wiki/feedback/rejection` — body
  `{concept_name, reason, page_id?, rejected_revision_id?}`.
- `GET /v1/projects/{id}/wiki/feedback/rejection?concept_name=…` — list
  pending feedback for a concept.

The full analyst-facing rejection UI lands in Inc 5 alongside the
approval workflow.
