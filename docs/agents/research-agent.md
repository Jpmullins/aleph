# Research agent (AIQ subsystem)

Aleph does not reimplement deep research. We vendor NVIDIA AIQ at
`vendor/aiq` and run it as a separate worker process. The Aleph side
talks to AIQ over HTTP via `aleph_aiq.client.AIQClient`.

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
- **Async job dispatch:** `POST /v1/jobs/async/agents` + SSE event
  stream proxied through `/v1/projects/{id}/aiq/jobs/{run_id}/stream`.

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
