# Research agent (AIQ subsystem)

Aleph does not reimplement deep research. We run NVIDIA AIQ as a separate
compose service (`aiq-server`) from the prebuilt image
`nvcr.io/nvidia/blueprint/aiq-agent:2.1.0` (not a local `vendor/aiq` build).
The Aleph side talks to AIQ over HTTP via `aleph_aiq.client.AIQClient`. The
`~/code/aiq` clone (v2.1.0) is the source reference for AIQ's config schema
and internals.

## What we use from AIQ

- **Agent pipeline:** Orchestrator → ShallowResearcher → DeepResearcher
  (DeepAgents-based) → Clarifier.
- **`data_source_registry`:** AIQ's typed source registry IS Aleph's
  connector registry. Every Aleph connector is authored as an `nat`
  function and surfaces inside AIQ.
- **Tokenomics:** AIQ reports per-phase token + cost stats. Aleph's
  `aleph_aiq.tokenomics_adapter` writes them into `ModelCall` +
  `CostLedgerEvent` so the project cost ledger is the single source of
  truth.
- **`citation_verification`:** `verify_citations`, `sanitize_report`.
  Aleph imports the matching contract from
  `aleph_wiki.citation_verification` (which mirrors AIQ's API).
- **Async job dispatch:** `POST /v1/jobs/async/submit {agent_type, input}`
  (`agent_type` = `deep_researcher` | `shallow_researcher`); status/results at
  `GET /v1/jobs/async/job/{id}` + `/report` + `/state`; SSE at
  `/v1/jobs/async/job/{id}/stream`. (The old `/v1/jobs/async/agents` path in
  earlier drafts does not exist on the 2.1.0 image.) Aleph's `AIQClient`
  (`aleph_aiq.client`) wraps these; the `aiq_synthesis_poll_job` worker polls
  to completion and feeds the report into `synthesis_workflow`.

## What changes from AIQ defaults

- **LLM transport.** Every AIQ `_type: nim` is rewritten to
  `_type: openai` with `base_url=LITELLM_BASE_URL` and
  `api_key=${INSIGHTS_LITELLM_API_KEY}`. AIQ thinks it's calling
  OpenAI; the gateway routes provider-by-provider.
- **No direct DB/S3 from AIQ.** All AIQ tool calls re-enter Aleph via
  `/internal/v1/aiq/*` callbacks (credentials, sources, model_calls,
  events). The AIQ container has no `DATABASE_URL` or MinIO secrets.
- **knowledge_layer disabled.** Aleph's wiki/RKS is the canonical KB.

## When AIQ runs

- **Manual `/synthesize`** — analyst issues `/synthesize <topic>`.
- **Auto-offered after a `synthesis_needed` coverage gap.** Inc 2's
  retrieval router flagged the gap; Inc 3 surfaces a `SynthesizeButton`
  prefilled with the missing concept.

The AIQ run is wrapped as an `AgentRun(kind="aiq_deep" | "aiq_shallow")`
on the Aleph side. Progress events accumulate as `AgentEvent` rows; the
chat surface subscribes via SSE for live updates.

## Output discipline

AIQ's report does **not** publish to the wiki directly. It is offered
as a `SynthesisProposal` (status=`pending`) plus draft wiki pages
(status=`draft`). Owner approval flips both to `approved` in one
transaction; rejection writes `RejectionFeedback` so the next
synthesis of the same topic can address the issue.
