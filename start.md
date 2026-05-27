Project Aleph Goal

Project Aleph is a living, interactive, multi-agent research environment.

It helps an analyst create, maintain, inspect, revise, visualize, and export a project-scoped body of knowledge. The system is built around three persistent layers:

Raw Knowledge Store
Stores source material, normalized source text, source metadata, provenance, access rules, hashes, parser outputs, datasets, and source-derived records.
Compiled LLM Wiki
A navigable, cited, revisioned project wiki generated from RKS. It is the primary human-readable knowledge layer and the primary assistant knowledge layer.
Interactive Analyst Workspace
A three-panel UI where the user chats with the assistant, navigates the wiki, reviews sources, tracks hypotheses, approves changes, inspects artifacts, and uses A2UI cards for charts, graphs, maps, forms, approvals, and visualizations.

Aleph is not “chat with documents.” It is a research operating environment with persistent knowledge, source lineage, structured revisions, auditable agent actions, and interactive analytical surfaces.

Success Conditions

Aleph is successful when all of the following are true.

Knowledge success

The system can:

Create a project from a title, description, constraints, and allowed connectors.
Collect raw source material into RKS.
Normalize raw source material into machine-readable and human-readable representations.
Build a project-scoped wiki from RKS.
Maintain internal wiki links, source links, artifact links, and evidence references.
Update the wiki incrementally when new source material appears.
Track claim-level provenance for important claims.
Identify unsupported, weakly supported, stale, or conflicting claims.
Let users approve or reject proposed wiki changes.
Preserve the entire history of source ingestion, wiki revisions, reviewer findings, artifacts, cards, approvals, and agent actions.
Interaction success

The user can:

Enter a project immediately after creation and see real work progressing.
Understand what the agents are doing, what has completed, what is blocked, and what needs approval.
Chat with the assistant while background work continues.
Navigate the wiki, sources, artifacts, hypotheses, notebook, review queue, and interactive workspace.
Ask the assistant to research more, update knowledge, generate hypotheses, build visualizations, and produce artifacts.
Interact with structured A2UI cards instead of relying only on chat.
Export charts, visualizations, reports, datasets, and source bundles with lineage.
Engineering success

The codebase must satisfy these conditions from the first merged increment:

Typed interfaces.
Database migrations.
Auth and project scoping.
Structured logging.
Langfuse tracing.
Test coverage for critical paths.
No fake implementations merged to main.
No silent failures.
No untracked state mutation.
No arbitrary LLM-generated executable code.
Every state-changing operation has an action ledger event.
Every agent run has a trace.
Every persisted object has a stable ID, owner/project scope, timestamps, and revision or hash where relevant.
Hard Engineering Rules

These are non-negotiable.

No placeholder code

Forbidden:

TODO: implement later
pass
NotImplementedError in reachable code
mock ingestion in production paths
fake progress
stubbed agents
hardcoded example projects
temporary schema
in-memory state for persisted workflows

Allowed only in tests:

mock model providers
mock connectors
fake object store
test-only fixtures
No “later hardening”

Security, tracing, provenance, and permissions are not cleanup work. They are core system behavior.

Every new feature must include:

database model
API contract
service implementation
tests
trace instrumentation
ledger events if state-changing
docs update
failure behavior
permission behavior
No hidden agent behavior

Agents must not mutate state directly. They call typed services. Services enforce permissions, tracing, validation, revisioning, and ledger writes.

Bad:

wiki_agent writes markdown file directly

Good:

wiki_agent proposes WikiPatch
wiki_service validates patch
wiki_service creates revision
ledger records revision
Langfuse records trace
UI receives event
Architecture
apps/
  web/                         React UI
  api/                         FastAPI or similar backend
  workers/                     async jobs and agent runners

packages/
  aleph-core/                  shared domain models
  aleph-db/                    migrations and repositories
  aleph-rks/                   raw knowledge store services
  aleph-wiki/                  wiki compiler and revision services
  aleph-agents/                LangGraph and Deep Agents
  aleph-retrieval/             hybrid search and reranking
  aleph-a2ui/                  card schema, registry, validators
  aleph-artifacts/             artifact generation and export
  aleph-observability/         Langfuse instrumentation helpers
  aleph-security/              auth, ACL, policy, permissions
  aleph-evals/                 system evals and regression suites
  aleph-docs/                  architecture and runbooks

Recommended stack:

Frontend: React, TypeScript, Tailwind, CopilotKit, A2UI renderer
Backend: Python, FastAPI, Pydantic, SQLAlchemy or SQLModel
Workflow: LangGraph
Agent harness: Deep Agents where useful
Database: Postgres
Object store: MinIO or S3-compatible storage
Vector search: pgvector initially, Qdrant only if needed by measured scale/filtering
Keyword search: Postgres FTS initially
Observability: Langfuse
Event stream: SSE or WebSocket
Queue/workers: Dramatiq, Celery, or Arq with Redis
Docs: Markdown in repo
Core Domain Objects

These are required early because they shape everything else.

Project
ProjectMember
AgentRun
AgentEvent
ActionLedgerEvent
Source
SourceVersion
SourceAsset
NormalizedDocument
DocumentChunk
RetrievalIndexRecord
WikiPage
WikiRevision
WikiClaim
WikiLink
ReviewRun
ReviewFinding
ApprovalRequest
AssistantThread
AssistantMessage
Artifact
ArtifactVersion
Dataset
DatasetVersion
Hypothesis
HypothesisVersion
InteractiveCard
InteractiveCardVersion
CardAction
RenderedAsset

Every object needs:

id
project_id where applicable
created_at
updated_at
created_by
access_scope
trace_id where applicable
ledger_action_id where applicable
Agent System
Bootstrap agent

Owns project initialization.

Responsibilities:

validate project title and description
create project work plan
start research workflow
monitor source collection
signal wiki build
open assistant workspace when usable
queue reviewer
emit progress events

Completion contract:

project exists
project status is accurate
research agent run exists
wiki agent run exists when RKS is ready
assistant thread exists
reviewer run is queued after wiki build
all actions traced
all state changes in ledger
Research agent

Owns source collection and RKS population.

Responsibilities:

plan seed research
use approved connectors only
fetch web pages or binary sources
store raw assets
normalize text and metadata
produce source records
produce normalized documents
emit source events
signal wiki agent

It never writes wiki pages.

Wiki agent

Owns wiki compilation.

Responsibilities:

read RKS
generate wiki outline
create source notes
create pages
create internal links
create claim records
attach evidence refs
commit page revisions
consume reviewer patches
update pages incrementally

It never overwrites wiki state without revisioning.

Reviewer agent

Owns assurance.

Responsibilities:

check unsupported claims
check citation mismatch
check stale sources
check contradictions
check broken links
check weak source quality
check chart/dataset/wiki consistency
produce findings
propose patches
route approvals

It should not silently edit high-impact content.

Assistant agent

Owns user collaboration.

Responsibilities:

answer questions
read active UI context
read wiki
read RKS when needed
request research
request wiki update
request review
create hypotheses
create A2UI cards
request artifact generation
explain provenance
Builder agent

Owns outputs.

Responsibilities:

create reports
create charts
create spreadsheets
create source packs
export artifacts
insert rendered card assets
preserve citations and lineage
Execution Decomposition for Coding Agent

These are not “versions.” They are production-complete build increments. Each increment must end with a working system, passing tests, updated docs, and a precise next-step note.

Increment 1: Repository, runtime, and quality gate

Build:

monorepo structure
backend app
frontend app
worker app
shared config
Docker Compose
Postgres
MinIO
Redis
Langfuse
migration framework
test framework
lint/typecheck/format
CI pipeline
local dev bootstrap command

Completion criteria:

developer can clone repo and run one command to start full local stack
API health check works
web app loads
worker connects
Postgres migration runs
MinIO bucket initialization runs
Langfuse is reachable
CI runs tests, lint, typecheck
docs explain local setup

Required docs update:

docs/engineering/local-development.md
docs/engineering/repo-structure.md
docs/engineering/quality-gates.md

Agent handoff note:

what was built
commands to run
tests passing
known constraints
next increment entry point
Increment 2: Core domain, auth boundary, and project service

Build:

Project model
ProjectMember model
ActionLedgerEvent model
AgentRun model
AgentEvent model
auth abstraction
project scoping middleware
project create/read/update APIs
project event stream
ledger service
Langfuse trace helper

Completion criteria:

project can be created
project has owner/member scope
all project mutations write ledger event
all project mutations emit trace
project status can be streamed to UI
tests prove unauthorized access fails
tests prove ledger records are immutable

Required docs:

docs/domain/projects.md
docs/domain/action-ledger.md
docs/security/project-scoping.md
Increment 3: RKS storage core

Build:

Source model
SourceVersion model
SourceAsset model
NormalizedDocument model
object storage service
content hashing
source registration API
file upload API
source metadata schema
source status lifecycle
RKS browser UI

Completion criteria:

user can upload a file into a project
raw file stored in object store
source record written to Postgres
content hash recorded
source version recorded
ledger event written
Langfuse trace created
UI shows source status
unauthorized project cannot read source

Required docs:

docs/domain/rks.md
docs/storage/object-store.md
docs/security/source-access.md
Increment 4: Document normalization pipeline

Build:

normalization job model
worker execution
PDF/DOCX/TXT/MD/HTML parser path
normalized markdown output
document metadata extraction
parser version tracking
failure state
retry behavior
normalization event stream

Completion criteria:

uploaded file is normalized
normalized document stored
source status updates correctly
parser failures are visible
retry works
trace includes parser spans
ledger records normalized output
UI shows normalized preview

Required docs:

docs/pipelines/normalization.md
docs/operations/failure-and-retry.md
Increment 5: Retrieval foundation

Build:

DocumentChunk model
chunking service
embedding service abstraction
pgvector storage
Postgres full-text index
retrieval API
hybrid retrieval service
retrieval trace schema
retrieval test corpus

Completion criteria:

normalized documents are chunked
chunks are embedded
keyword and vector retrieval both work
hybrid retrieval returns source refs
retrieval respects project scope
retrieval traces query, filters, top_k, selected refs
tests cover exact-match, semantic-match, and permission filtering

Required docs:

docs/retrieval/indexing.md
docs/retrieval/hybrid-search.md
docs/observability/retrieval-traces.md
Increment 6: Wiki data model and revision service

Build:

WikiPage model
WikiRevision model
WikiClaim model
WikiLink model
wiki namespace
revision commit service
wiki read APIs
wiki page tree APIs
wiki renderer in UI
claim provenance schema

Completion criteria:

wiki page can be created through service
wiki revision is immutable
page tree renders
claim records link to source refs
wiki page cannot be overwritten directly
all revisions write ledger events
all wiki writes are traced

Required docs:

docs/domain/wiki.md
docs/domain/claims-and-provenance.md
docs/wiki/revisioning.md
Increment 7: Wiki compiler agent

Build:

LangGraph wiki workflow
Deep Agent harness if useful
outline generation node
source note generation node
page generation node
linking node
claim extraction node
citation attachment node
wiki commit node
progress events

Completion criteria:

given normalized RKS documents, agent builds initial wiki
pages have source refs
claims have evidence refs where possible
internal links are generated
agent emits progress events
trace shows each workflow node
failures leave project in recoverable state
tests run compiler against fixture corpus

Required docs:

docs/agents/wiki-agent.md
docs/wiki/compiler-workflow.md
docs/prompts/wiki-prompts.md
Increment 8: Project bootstrapping workflow

Build:

Bootstrap LangGraph workflow
project initialization state machine
research seed task creation
normalization wait logic
wiki build trigger
assistant thread creation
reviewer queue trigger
front-end bootstrapping progress screen

Completion criteria:

create project launches bootstrap workflow
user sees real phase updates
source ingestion and wiki build are visible
assistant screen becomes available when wiki is usable
reviewer run is queued
all transitions are traced and ledgered
no fake progress

Required docs:

docs/agents/bootstrap-agent.md
docs/workflows/project-bootstrap.md
docs/ui/progress-ux.md
Increment 9: Research agent and connector framework

Build:

connector interface
web connector
SearXNG connector if configured
direct URL fetcher
binary downloader
web snapshotter
normalized markdown conversion
source deduplication
connector permission checks
research planning workflow

Completion criteria:

research agent can collect seed sources from allowed connectors
web pages become source assets plus normalized markdown
binary downloads are stored when available
source metadata includes URL and retrieval timestamp
disallowed connector use fails visibly
all connector calls traced
all created sources ledgered

Required docs:

docs/agents/research-agent.md
docs/connectors/connector-contract.md
docs/connectors/web-ingestion.md
Increment 10: Assistant screen and persistent chat

Build:

three-panel layout
left project navigation
center assistant chat
activity card
right knowledge tabs
assistant thread model
message model
assistant LangGraph workflow
retrieval over wiki and RKS
active UI context injection

Completion criteria:

user can chat with assistant inside project
assistant answers from wiki/RKS with refs
assistant sees active wiki/source/artifact context
activity card shows current plan and actions
threads persist
all assistant runs traced
all state-changing requests go through services

Required docs:

docs/ui/assistant-screen.md
docs/agents/assistant-agent.md
docs/copilotkit/context-bridge.md
Increment 11: Reviewer agent and approval workflow

Build:

ReviewRun model
ReviewFinding model
ApprovalRequest model
review workflow
claim verification node
citation validation node
contradiction detection node
patch proposal node
approval queue UI
approve/reject APIs
wiki patch application service

Completion criteria:

reviewer can inspect wiki against RKS
reviewer produces structured findings
findings appear in review queue
user can approve or reject patch
approved patch creates wiki revision
rejected patch is recorded
all approvals ledgered
all review steps traced

Required docs:

docs/agents/reviewer-agent.md
docs/review/finding-schema.md
docs/review/approval-workflow.md
Increment 12: Interactive Workspace and A2UI foundation

Build:

InteractiveCard model
InteractiveCardVersion model
CardAction model
A2UI schema validator
component registry
permission model
card renderer
card action router
approval card
review finding card
table card
chart card

Completion criteria:

assistant can propose a card
card validates against schema
card renders in Interactive Workspace
card action creates action request
card cannot mutate state directly
invalid card is rejected and traced
card versions are persisted
card actions are ledgered

Required docs:

docs/a2ui/interactive-workspace.md
docs/a2ui/card-registry.md
docs/a2ui/card-permissions.md
Increment 13: Dataset and visualization layer

Build:

Dataset model
DatasetVersion model
Observation model
metric definition model
dataset import service
dataset editing UI
Vega-Lite chart card
React Flow graph card
MapLibre map card
data snapshot service

Completion criteria:

structured benchmark data can be stored
chart card can bind to dataset version
chart uses immutable data snapshot
graph card can render architecture/workflow
map card can render approved geo data
visualization provenance is visible
all card generations traced

Required docs:

docs/domain/datasets.md
docs/a2ui/visualization-cards.md
docs/provenance/data-snapshots.md
Increment 14: Rendered asset and artifact export service

Build:

RenderedAsset model
Playwright render service
PNG export
SVG export where supported
PDF snapshot export
artifact object storage
artifact lineage model
artifact browser UI

Completion criteria:

card can be exported to image
rendered asset records card version and data snapshot
asset stored in object store
artifact lineage visible
exports are reproducible from stored spec and snapshot
render failures are visible
render process is sandboxed

Required docs:

docs/artifacts/rendered-assets.md
docs/artifacts/export-service.md
docs/security/render-sandbox.md
Increment 15: Builder agent

Build:

Artifact model
ArtifactVersion model
template registry
builder workflow
markdown report export
PDF export
DOCX export
source appendix generation
chart insertion
artifact approval workflow

Completion criteria:

builder creates report from approved wiki pages/cards/datasets
report includes citations and artifact lineage
generated artifact is stored
artifact can be revised
export is traced
artifact creation writes ledger events

Required docs:

docs/agents/builder-agent.md
docs/artifacts/report-generation.md
docs/templates/template-contract.md
Increment 16: Hypotheses and notebook

Build:

Hypothesis model
HypothesisVersion model
hypothesis evidence links
confidence/update fields
hypothesis cards
notebook document model
notebook cells or markdown sections
assistant-assisted hypothesis editing
reviewer checks over hypotheses

Completion criteria:

user can create and update hypotheses
hypotheses link to wiki claims and RKS sources
assistant can propose hypothesis updates
updates require approval if configured
hypothesis history is preserved
notebook entries persist and link to sources/cards

Required docs:

docs/domain/hypotheses.md
docs/domain/notebook.md
docs/ui/hypotheses-and-notebook.md
Increment 17: End-to-end assurance and eval suite

Build:

fixture project corpus
end-to-end project creation test
retrieval evals
wiki provenance evals
reviewer evals
assistant answer evals
A2UI validation evals
artifact lineage evals
permission leakage tests
regression test runner

Completion criteria:

one command runs full assurance suite
eval reports are stored
critical regressions fail CI
permission leakage fails CI
unsupported claim threshold is enforced
broken provenance fails CI

Required docs:

docs/evals/system-evals.md
docs/evals/regression-suite.md
docs/security/permission-tests.md
Required Coding Agent Operating Procedure

The coding agent must follow this after every increment.

1. Update implementation log

Create or update:

docs/implementation-log.md

Entry format:

## Increment N: Name

Completed:
- ...

Key files changed:
- ...

Database migrations:
- ...

Tests added:
- ...

Trace/ledger behavior added:
- ...

Manual verification:
- ...

Known issues:
- ...

Next increment:
- ...
2. Update architecture docs

Every new subsystem gets a doc. Existing docs must be revised when behavior changes.

No undocumented subsystem is acceptable.

3. Update runbook

If an operator needs to start, debug, retry, inspect, or repair something, the runbook must say how.

docs/operations/runbook.md
4. Update API docs

Every API endpoint must include:

method
path
request schema
response schema
auth behavior
error behavior
trace behavior
ledger behavior if state-changing
5. Update test report

Every increment ends with:

test command
lint command
typecheck command
migration command
local smoke test result
Definition of Done for Any Increment

An increment is not complete unless:

all code is real
all reachable paths are implemented
all DB migrations are included
all critical paths are tested
permissions are enforced
Langfuse traces exist
ledger events exist for mutations
errors are explicit
UI shows real state
docs are updated
runbook is updated
no placeholder code remains
What to Put in the Coding Agent System Prompt

Use this as the controlling instruction.

You are building Project Aleph, a production-grade living multi-agent research environment.

Do not create placeholders, fake services, unreachable stubs, TODO implementations, or temporary scaffolding. Every merged increment must leave the system working, tested, documented, traceable, and recoverable.

You must build in logically ordered production-complete increments. At the end of each increment, update docs/implementation-log.md with what was completed, files changed, migrations added, tests added, trace and ledger behavior added, manual verification steps, known issues, and the next recommended increment.

All state-changing operations must go through typed services. Agents must not mutate persistent state directly. Every state-changing operation must create an action ledger event. Every agent run, model call, tool call, retrieval call, approval, card generation, and artifact export must be traceable in Langfuse.

Security, permissions, provenance, revisioning, and observability are first-order features. Do not defer them.

Use LangGraph for long-running stateful workflows. Use Deep Agents where its planning, subagent, filesystem, memory, skill, sandbox, or human-in-the-loop capabilities are useful. Use Langfuse for observability. Use A2UI only through a governed declarative card registry. Do not execute arbitrary LLM-generated UI code.

After each increment, run tests, typechecks, linting, migrations, and a local smoke test. Do not proceed until the increment satisfies its completion criteria.
The Main Correction to the Spec

The old framing was too product-roadmap-like.

The right framing is:

Build the permanent foundations first.
Then add each capability only when its full data model, service layer, UI, observability, ledger behavior, tests, and docs can be completed.

That is not slow. It is the fastest way to avoid building a pile of demos that cannot become Aleph.