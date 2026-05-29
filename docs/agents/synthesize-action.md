# `/synthesize` action

End-to-end:

```
User: /synthesize "Transformer capacity in Region X"
  │
  ▼
POST /v1/projects/{id}/synthesize
  body: { topic, depth, allowed_connectors? }
  ▼
aleph-api creates AgentRun(kind="aiq_{depth}"), issues service token,
dispatches to aiq-server (POST /v1/jobs/async/submit), AND enqueues the
`aiq_synthesis_poll_job` worker (re-enqueue-with-defer; survives long deep
runs). Returns { agent_run_id, aiq_job_id, dispatched }.
(The Live agent's `start_research` tool calls this same endpoint, so research
can be kicked off conversationally.)
  ▼
AIQ Orchestrator + ShallowResearcher + DeepResearcher run.
For every connector call, AIQ POSTs /internal/v1/aiq/credentials/{kind}
to fetch the API key, then calls the upstream API, then POSTs
/internal/v1/aiq/sources to persist the result as a Source.
For every LLM call, AIQ POSTs /internal/v1/aiq/model-calls
(bulk; tokenomics adapter) so cost is ledgered.
  ▼
AIQ returns a structured report:
  { body_md, sources, citations_by_marker, claims }
  ▼
the `aiq_synthesis_poll_job` worker (aleph-workers) polls the job to
completion, fetches /report + /state, parses them into an `AIQReport`
(remapping AIQ's numeric [N] citation markers → [cN]), and runs
`aleph_wiki.synthesis_workflow.SynthesisWorkflow`:
  concept_normalize → citation_verification → wikilink_resolve →
  commit_revision (status=draft + SynthesisProposal row) → wiki_index_update.
  ▼
SynthesisProposal rows land with status=pending.
The chat surface shows a SynthesisDraftPreview ("Synthesized N pages
and M revisions. Review and approve?").
  ▼
Owner approves: POST /v1/projects/{id}/synthesis-proposals/{id}/approve
  → page.status = approved, ApprovalDecision recorded.
Owner rejects: POST .../reject with reason
  → page.status = archived, RejectionFeedback row created,
    next /synthesize on the same topic gets the reason as context.
```

## Failure modes

- **AIQ unreachable** at dispatch: `synthesize` returns
  `dispatched=false` with the AgentRun in `pending`. Operator brings
  AIQ up; retry via `POST /v1/projects/{id}/aiq/jobs/{run_id}/retry`
  (lands in a follow-on).
- **Citation verification fails** for an AIQ-authored report: the
  synthesis workflow raises `CitationVerificationFailure` and the
  AgentRun ends `failed` with the missing markers listed in
  `error_text`. The user sees a clear error in chat.
- **Clarifier loop**: AIQ Clarifier asks a question; the chat surface
  shows it; the user answers; `POST /v1/projects/{id}/aiq/jobs/{id}/clarify`
  forwards the answer to AIQ which resumes the run.

## What's not in Inc 3

- The full BriefsSurface (A2UI) — lands in Inc 4. Inc 3 ships the
  synthesis proposals list as a plain JSON list at
  `GET /v1/projects/{id}/synthesis-proposals`.
- MechanicalReviewer's broader checks — lands in Inc 5. The
  citation-verification node is the only mechanical check active in
  Inc 3.
- artificialanalysis.ai connector — lands in Inc 6 alongside Datasets.
