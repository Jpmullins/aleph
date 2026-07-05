# Increment 3 — AIQ Subsystem + Connector Roster + `--synthesize`

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md`
**Depends on:** Inc 0, Inc 1, Inc 2
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 3.1 Scope

Increment 3 wires in NVIDIA AIQ as the **research-agent subsystem**, brings the full connector roster online (so the wiki can grow from web/papers/feeds/structured APIs, not just uploads), and turns the Inc 2 `synthesis_needed` flag into an actual action: `/synthesize` triggers AIQ DeepResearcher → wiki agent → owner approval → new wiki content.

This is the increment where Aleph stops being limited to "what you uploaded" and starts being a research engine that adds to its own knowledge.

### In scope

- **AIQ vendoring** as a git submodule at `vendor/aiq` pinned to current release tag (decision §1.5)
- AIQ run as `aiq-server` container in compose
- AIQ LLM config rewritten to `_type: openai` with `base_url=LITELLM_BASE_URL`
- AIQ tokenomics adapter writing into Aleph `CostLedger`
- AIQ orchestrator/shallow/deep/clarifier invokable by the assistant
- Service-token auth boundary: AIQ holds no Postgres/S3 creds; tool calls re-enter aleph-api
- `ConnectorCredential` model (encrypted at rest) — envelope encryption via libsodium for local dev, KMS for prod
- All Inc 3 connectors authored as `nat` functions registered with AIQ's `data_source_registry`:
  - **Adopted from AIQ:** Tavily, Exa, Serper (Google Scholar)
  - **Aleph-built:** arXiv, Semantic Scholar, OpenAlex, Lens.org (disabled by default — credential pending), RSS, HuggingFace Hub
  - **Upload connector** re-registered as a `nat` function (was in-process in Inc 1)
- Citation verification: AIQ's `citation_verification.verify_citations` integrated as a pre-commit check inside the wiki agent (used today by mechanical pre-flight; full MechanicalReviewer is Inc 5)
- `/synthesize` slash command wired in chat
- Synthesis-proposal flow: AIQ result → wiki agent → wiki commit with `status="draft"` → owner approval API → status flips to `"approved"` (badge in Briefs lands in Inc 4 + reviewer in Inc 5)
- Async deep research jobs exposed as `AgentRun` rows with SSE progress

### Explicitly out of scope

- artificialanalysis.ai connector (output_kind=`dataset_rows`) — **moved to Inc 6** where `Dataset`/`Observation` land. Inc 3 ships only `document` output_kind connectors.
- A2UI surfaces and the BriefsSurface badge UI → Inc 4
- Full Reviewer agents (Mechanical + Editorial) → Inc 5; this increment ships only the `citation_verification` pre-flight, integrated inside the wiki agent
- Datasets, charts → Inc 6
- Builder, artifacts → Inc 7

### Dependencies

- Inc 0: LiteLLMClient, LedgerWriter, Principal/agent_tokens, OTEL+Langfuse, ModelProfile, Budget
- Inc 1: Source/SourceVersion/SourceAsset/NormalizedDocument/DocumentChunk/SourcePage/WikiPage/WikiRevision/WikiClaim/Citation/WikiIndex; the wiki agent's ingest workflow; `Connector` / `ConnectorBinding`
- Inc 2: `WikiFirstRetrievalRouter`, `synthesis_requests` slot on `AssistantMessage.retrieval_jsonb`

### Downstream

- Inc 4: BriefsSurface lists synthesis drafts via the API exposed here.
- Inc 5: Editorial reviewer operates on synthesis drafts; MechanicalReviewer expands the citation_verification check.
- Inc 6: artificialanalysis.ai connector lands as the first `dataset_rows` connector using the framework established here.

---

## 3.2 Repository changes

```
vendor/
└── aiq/                                # git submodule, pinned to current release tag

packages/
├── aleph-aiq/                          # AIQ integration (Aleph's side)
│   └── src/aleph_aiq/
│       ├── __init__.py
│       ├── client.py                   # AIQ REST client + SSE consumer
│       ├── config_generator.py         # emits AIQ YAML configs with LiteLLM bindings
│       ├── tokenomics_adapter.py       # AIQ → CostLedger writer
│       ├── auth_bridge.py              # AIQ service-token issuance + reverse-callback verify
│       └── job_service.py              # AIQ job → AgentRun lifecycle
├── aleph-connectors/                   # connector framework + all connector plugins
│   └── src/aleph_connectors/
│       ├── __init__.py
│       ├── base.py                     # ConnectorBase Protocol + ConnectorResult/RawPayload/etc.
│       ├── registry.py                 # nat function registration
│       ├── credentials.py              # ConnectorCredential model + encryption
│       ├── tavily/
│       ├── exa/
│       ├── serper/
│       ├── arxiv/
│       ├── semantic_scholar/
│       ├── openalex/
│       ├── lens/
│       ├── rss/
│       ├── huggingface_hub/
│       └── upload/                     # re-implemented as nat function
└── aleph-wiki/                         # extended
    └── src/aleph_wiki/agent/nodes/
        └── citation_verification.py    # new node: AIQ's verify_citations integrated

apps/api/src/aleph_api/routes/
├── synthesize.py                       # POST /synthesize, GET progress, POST approve
├── connector_credentials.py            # CRUD (owner)
└── aiq_jobs.py                         # status proxy + SSE proxy

apps/web/src/
├── components/
│   ├── SynthesizeButton.tsx            # in chat composer
│   ├── SynthesisProgressCard.tsx       # transient activity card content
│   └── SynthesisDraftPreview.tsx       # before approval
└── (no new routes; chat surface gains synthesize action)

deploy/compose/docker-compose.yml       # adds aiq-server service
```

---

## 3.3 AIQ vendoring (decision)

**Decision (locked here):** vendor AIQ as a **git submodule** at `vendor/aiq`, pinned to current release tag verified at the start of this increment via `gh release list -R NVIDIA-AI-Blueprints/aiq`.

Rationale: AIQ is moving; PyPI dep would require unpredictable upgrade churn; fork would diverge. Submodule keeps a known tag, lets us patch on a branch when needed, and rolls forward as new tags land per top-level §15.6.

The submodule is excluded from CI builds by default; we install AIQ as an editable PyPI package from the submodule directory:

```toml
# apps/api/pyproject.toml (snippet)
[tool.uv.sources]
aiq = { path = "../../vendor/aiq", editable = true }
```

A `scripts/update-aiq.sh` helper bumps the submodule and verifies the AIQ test suite still passes; renovate-bot opens upgrade PRs that run this script in CI.

---

## 3.4 AIQ runtime configuration

The AIQ server runs in compose as `aiq-server` on port 8001. Its configuration is generated by `aleph_aiq.config_generator` at startup and mounted at `/etc/aiq/config.yml`.

### Generated config shape

`config_generator.emit(project_id: UUID) -> str` produces a YAML config based on the project's `ModelProfile` and `ConnectorBinding`s. Example (skeleton; the actual file is fully populated by the generator):

```yaml
general:
  telemetry:
    tracing:
      otel:
        _type: otlp
        endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}
        # AIQ writes OTEL; aleph-observability picks up and forwards to Langfuse

llms:
  # All LLM bindings point at the Insights LiteLLM Gateway.
  # Model names come from the project's ModelProfile (resolved at config-gen time).
  intent_classifier:
    _type: openai
    model_name: ${MODEL_CLASSIFICATION}  # e.g. claude-haiku-4-5
    base_url: ${LITELLM_BASE_URL}
    api_key: ${INSIGHTS_LITELLM_API_KEY}
    temperature: 0.0
    max_retries: 5
  shallow_researcher_llm:
    _type: openai
    model_name: ${MODEL_SYNTHESIS}       # e.g. claude-opus-4-7 in prod
    base_url: ${LITELLM_BASE_URL}
    api_key: ${INSIGHTS_LITELLM_API_KEY}
    max_retries: 5
  deep_orchestrator_llm:
    _type: openai
    model_name: ${MODEL_SYNTHESIS}
    base_url: ${LITELLM_BASE_URL}
    api_key: ${INSIGHTS_LITELLM_API_KEY}
    max_retries: 10
  # ... clarifier, deep researcher subagents

functions:
  data_sources:
    _type: data_source_registry
    sources:
      # populated from ConnectorBinding rows where enabled=true
      - id: web_search
        name: "Web Search"
        tools: [tavily_web_search, exa_web_search]
      - id: paper_search
        name: "Academic Papers"
        tools: [serper_paper_search, arxiv_search, semantic_scholar_search, openalex_search]
      # - id: patents_legal — disabled in Inc 3 (Lens.org credential pending)
      # ... etc.

  tavily_web_search:
    _type: tavily_web_search
    # api_key is NOT in YAML; the nat function fetches it from aleph-api via callback
    max_results: 5
    max_content_length: 1000
  # ... one block per registered connector tool

  intent_classifier: {_type: intent_classifier, llm: intent_classifier}
  shallow_researcher: {_type: shallow_researcher, llm: shallow_researcher_llm}
  deep_researcher:    {_type: deep_researcher, llm: deep_orchestrator_llm}
  clarifier:          {_type: clarifier, llm: shallow_researcher_llm}
```

### Per-project config

The config generator emits a config *per project*. AIQ serves one project at a time per call: incoming requests carry `X-Aleph-Project-Id` and the AIQ server's HTTP middleware swaps the config context. Implementation: a thin AIQ wrapper (in `aleph-aiq`) maintains an LRU cache of compiled configs keyed by `project_id`, invalidating on `ModelProfile`/`ConnectorBinding` change events from aleph-api (SSE channel).

---

## 3.5 ConnectorCredential

```python
# packages/aleph-connectors/src/aleph_connectors/credentials.py

class ConnectorCredential(CommonColumns, Base):
    """Encrypted credential for a (project, connector) pair.
    Per-project override; deployment env carries dev-default fallbacks (Inc 0 §10.4)."""
    __tablename__ = "connector_credentials"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    connector_id: Mapped[UUID] = mapped_column(nullable=False)
    cipher_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # libsodium sealed box (local) or KMS-wrapped DEK + AES-GCM ciphertext (prod)
    cipher_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    # libsodium-sealed | kms-aes-gcm
    kms_key_arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("project_id", "connector_id"),)
```

Encryption:

- **Local dev:** libsodium sealed box with the project's owner-derived keypair (key material lives in MinIO-encrypted secret at `s3://bucket/projects/{project_id}/keys/connector.sealed`).
- **Production:** envelope-encrypt with KMS — DEK wrapped by KMS key per project; ciphertext stored in `cipher_blob` along with the wrapped DEK header.

A credential is **never returned** by any HTTP endpoint. The only consumer is the connector plugin running inside AIQ via the callback (next section).

`ConnectorCredentialService`:
- `create_or_update(project_id, connector_kind, plaintext)` — owner-only; ledgered (key plaintext not in ledger payload)
- `decrypt_for_callback(project_id, connector_kind, agent_token)` — only callable from AIQ's callback path; verifies the agent token corresponds to an `AgentRun` for this project
- `rotate(project_id, connector_kind)` — owner; ledgered

If no project-specific credential exists, the service falls back to the deployment env var per the top-level §10.4 dev defaults (`TAVILY_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, etc.).

---

## 3.6 Tokenomics adapter (AIQ → CostLedger)

AIQ's tokenomics module produces per-phase `PhaseStats` records (orchestrator, planner-agent, researcher-phase). After every AIQ job completes (or streams events during long runs), `aleph_aiq.tokenomics_adapter`:

1. Reads AIQ's per-call telemetry from the AIQ event store.
2. For each LLM call AIQ made, writes a `ModelCall` + `CostLedgerEvent` row in aleph-api via the auth-bridged callback.
3. Sets `ModelCall.purpose = "aiq.<phase>"` (e.g. `aiq.deep_researcher`, `aiq.shallow_researcher`).
4. Sets `ModelCall.agent_run_id` to the Aleph-side `AgentRun` row created for the AIQ job.

Cache-hit-rate is preserved (AIQ tokenomics reports it; we record both raw and cached input tokens).

**Result:** the project cost ledger is the single source of truth for total spend, including AIQ-driven LLM calls. Budget enforcement applies uniformly.

---

## 3.7 Auth bridge (AIQ ↔ aleph-api)

```
aleph-api ──issues service token──> aiq-server (per AgentRun, 1h TTL)
                                         │
                                         │ runs research
                                         │ tool calls (connectors)
                                         ▼
aiq-server ──callback HTTP──────────> aleph-api
                                       (verifies service token,
                                        resolves project, returns
                                        credentials, persists Sources)
```

### Callback endpoints (used only by AIQ via service token)

- `POST /internal/v1/aiq/credentials/{connector_kind}` — body `{agent_token}`; returns decrypted credential ONLY if token is valid, scoped to the right project, and the connector is allowlisted
- `POST /internal/v1/aiq/sources` — body `{connector_kind, external_id, title, url, metadata, raw_bytes (base64) | raw_uri}`; creates `Source` + `SourceVersion` + `SourceAsset`; enqueues normalization; returns `{source_id, source_short_id}` so AIQ can cite it
- `POST /internal/v1/aiq/model-calls` — body `{calls: [{model, phase, in_tokens, cached_tokens, out_tokens, cost_usd, latency_ms, ...}]}`; bulk-writes `ModelCall` + `CostLedgerEvent`
- `POST /internal/v1/aiq/events` — bulk write of `AgentEvent` rows for progress visibility

These endpoints live under `/internal/v1/` and require an `X-Aleph-Service-Token` header (signed JWT, AIQ holds it via env). They are NOT exposed publicly — the compose network restricts `/internal/*` to internal services.

### Service token shape

JWT signed by aleph-api with: `iss=aleph-api`, `sub=aiq-server`, `agent_run_id`, `project_id`, `principal_user_id`, `exp` ≤ 1h, `scope=aiq.full`. Issued by `aleph_aiq.auth_bridge.issue(agent_run_id)`. Renewed at the 50-minute mark via a background job.

---

## 3.8 Connector implementations

Every connector lives in `packages/aleph-connectors/src/aleph_connectors/<name>/` with this shape:

```
<name>/
├── __init__.py
├── register.py        # @nat.register_function plus ConnectorBase impl
├── api_client.py      # HTTP client for the upstream API
├── normalize.py       # raw → markdown + structure + quality_flags
├── metadata.py        # Pydantic metadata schema (mirrored in Connector.metadata_schema_jsonb)
└── tests/
    ├── test_search.py
    ├── test_fetch.py
    └── test_normalize.py
```

### Per-connector contracts

| Connector | `kind` | `output_kind` | Auth | Key behavior |
|---|---|---|---|---|
| **Upload** | `upload` | `document` | none | Re-registered from Inc 1 as a `nat` function so AIQ data_source_registry sees it; no functional change |
| **Tavily** | `web_search` | `document` | API key | `search` returns top-k web results; `fetch` retrieves + snapshots HTML; `normalize` runs readability → markdown |
| **Exa** | `web_search` | `document` | API key | Same shape as Tavily; complementary semantic search |
| **Serper** | `paper_search` | `document` | API key | Google Scholar via Serper; returns paper metadata; PDF fetch best-effort |
| **arXiv** | `paper_search` | `document` | none (rate-limited) | OAI-PMH search; fetches PDF directly from arXiv; preserves DOI and arXiv id in metadata |
| **Semantic Scholar** | `paper_search` | `document` | optional key | Graph API: returns paper metadata + citation graph hint (stored in `Source.source_metadata_jsonb.citations` for Inc 5+ use) |
| **OpenAlex** | `paper_search` | `document` | mailto tag (no key) | Open scholarly graph; returns paper metadata + citation graph hint |
| **Lens.org** | `paper_search` | `document` + patents | API key | **Disabled at deploy** (`Connector.enabled_by_default=False`) until credential is provided; registration exists |
| **RSS** | `feed` | `document` | none | Polls configured feed URLs; each item becomes a Source; uses readability for content extraction |
| **HuggingFace Hub** | `model_repo` | `document` | optional | Returns model/dataset/paper cards as markdown sources |

Each `register.py` declares the `nat.FunctionBaseConfig` subclass and the registered async function. Example pattern (Tavily):

```python
# packages/aleph-connectors/src/aleph_connectors/tavily/register.py

class TavilyConfig(FunctionBaseConfig, name="tavily_web_search"):
    max_results: int = Field(default=5)
    max_content_length: int = Field(default=1000)
    advanced_search: bool = Field(default=False)

@register_function(config_type=TavilyConfig)
async def tavily_web_search(tool_config: TavilyConfig, builder: Builder):
    # Inside AIQ runtime. Fetch credential via the callback (NOT from env).
    credential = await callback_client.get_credential(
        connector_kind="tavily",
        agent_token=context.agent_token,
    )
    client = TavilyClient(api_key=credential)

    async def _search(query: str) -> list[ConnectorResult]:
        results = await client.search(query, max_results=tool_config.max_results)
        # AIQ contract: tool returns a list of result objects
        # AIQ will let DeepResearcher decide what to fetch
        return [
            ConnectorResult(
                external_id=r.url,
                title=r.title,
                url=r.url,
                snippet=r.snippet,
                metadata={"score": r.score, ...},
            ) for r in results
        ]

    # When AIQ decides to actually fetch + persist a result, the persist call goes
    # to /internal/v1/aiq/sources, returning a Source short_id that AIQ uses for
    # citation in its report.
    return _search
```

For each connector, the `fetch + persist` step writes a `Source` via the callback. Normalization runs in the aleph-workers normalize job (the same Inc 1 path) — AIQ does not normalize; it just supplies the raw payload.

### Connector tests

Each connector has unit tests with an httpx mock for the upstream API. Integration tests run against the real upstream only in a nightly job (`.github/workflows/connectors-live.yml`) — not on every PR, to avoid rate limits.

---

## 3.9 Citation verification (pre-flight)

AIQ ships `src/aiq_agent/common/citation_verification.py` with `verify_citations`, `sanitize_report`, `EmptySourceRegistryError`. We integrate this as a **new node** in the wiki agent (Inc 1 §1.8 had 7 nodes; Inc 3 inserts an 8th):

7.5. **`citation_verification`** — called between `wikilink_resolve` and `wiki_index_update`. For each draft page, runs `verify_citations(body_md, source_registry)` where `source_registry` is the set of `Source` + `Citation` rows touched by this compile. If any `[c…]` marker fails verification (no real source backing it), the page is flagged:

- For wiki-agent-authored drafts (ingest path): the offending citation is removed and replaced with `[c?]` (unverified). The agent retries the compose node once with the failure reasons fed back. Second failure leaves `[c?]` in place; the page lands with `WikiClaim.confidence="uncited"` for affected claims.
- For AIQ-authored synthesis proposals: verification failure blocks commit. The synthesis returns a structured error to the user with which citations failed. Owner can retry with different connectors.

This is the citation-matching layer of the future MechanicalReviewer; the broader Mechanical pass (broken/stale wikilinks, hash, dupe, freshness) lands in Inc 5.

---

## 3.10 `/synthesize` action

### Trigger

The chat composer (Inc 2) is extended with a `/synthesize` slash command. Activated:

- Manually by the user typing `/synthesize <topic>` (the topic is the concept the user wants synthesized).
- Automatically offered when the prior assistant turn returned `coverage_judgment="synthesis_needed"` — an inline `SynthesizeButton` appears with prefilled topic from the synthesis_requests.

### Flow

```
User issues /synthesize "Transformer capacity in Region X"
    │
    ▼
aleph-api POST /v1/projects/{id}/synthesize
    body: {topic, allowed_connectors[]}
    │
    ▼
Create AgentRun(kind="aiq_deep", correlation_id=...)
    │
    ▼
aleph-aiq client POSTs /v1/jobs/async/agents to aiq-server
    body: {query, depth=deep, project_id, allowed_data_sources, service_token}
    │
    ▼
AIQ Orchestrator + Clarifier (HITL clarification surfaced via aleph-api SSE
    to chat — user can answer clarifying questions inline)
    │
    ▼
AIQ DeepResearcher runs (planning + iterating + tool calls)
    every connector fetch → callback to aleph-api → Source persisted
    every LLM call → callback for ModelCall + CostLedgerEvent
    progress events → callback for AgentEvent → SSE to chat
    │
    ▼
AIQ produces a structured report:
    {body_md, sources: [...], citations: [...], section_outline: [...]}
    │
    ▼
aleph-api receives report; passes to wiki agent's synthesis path
    │
    ▼
Wiki agent runs `synthesis_compose` workflow:
    nodes: concept_normalize, citation_verification, wikilink_resolve,
           wiki_index_update, commit_revision
    Produces draft pages (kind="synthesis", status="draft")
    │
    ▼
Synthesis drafts land. Returned to chat with a SynthesisDraftPreview card.
Owner approval action: POST /v1/wiki/pages/{id}/approve (flips status to "approved")
or POST /v1/wiki/pages/{id}/reject (deletes draft, writes RejectionFeedback)
    │
    ▼
On approve: page is now part of the wiki, eligible for retrieval router
On reject: feedback wired; AIQ can retry with different params
```

### `synthesis_compose` workflow

A new LangGraph workflow in `aleph_wiki.agent.synthesis_workflow`:

```python
class SynthesisState(TypedDict):
    agent_run_id: UUID
    project_id: UUID
    topic: str
    aiq_report: AIQReport
    profile: ModelProfile
    rejection_context: list[RejectionFeedback]
    drafts: list[WikiPageDraft] | None
    committed_revision_ids: list[UUID] | None
```

Nodes:

1. `concept_normalize` — alias-resolve concepts; identify whether topic already has a WikiPage or needs a new one
2. `citation_verification` — same as Inc 3 §3.9 (reused)
3. `wikilink_resolve` — same as Inc 1
4. `wiki_index_update` — same
5. `commit_revision` — same, but commits with `status="draft"` (not `"approved"`); also writes a `SynthesisProposal` row (see below)

```python
class SynthesisProposal(CommonColumns, Base):
    __tablename__ = "synthesis_proposals"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    agent_run_id: Mapped[UUID] = mapped_column(nullable=False)
    topic: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # pending | approved | rejected
    approval_decision_id: Mapped[UUID | None] = mapped_column(nullable=True)
```

The `SynthesisProposal` row is what Inc 4's BriefsSurface will list. Inc 5's EditorialReviewer will operate on it too. For Inc 3, owner approval is via direct API endpoint.

### Migration

`<timestamp>_inc3_aiq_synthesis.py`:
- Creates `ConnectorCredential`, `SynthesisProposal`
- Adds `enabled_by_default` flag on `Connector` (defaulting false; flipped per-connector via seed update)
- Seeds `Connector` rows for all Inc 3 connectors with `metadata_schema_jsonb` populated

---

## 3.11 HTTP API

All under `/v1/projects/{project_id}/`.

### Connector credentials

- `GET /connector-credentials` — owner; list which connectors have project-specific creds (returns names only, no plaintext)
- `PUT /connector-credentials/{connector_kind}` — owner; body `{plaintext}`; encrypts and stores; ledgered (plaintext NEVER in ledger)
- `DELETE /connector-credentials/{connector_kind}` — owner; removes project-specific cred; deployment-env fallback resumes
- `POST /connector-credentials/{connector_kind}/rotate` — owner; ledgered

### AIQ jobs

- `POST /aiq/jobs` — body `{topic, depth, allowed_connectors[]}`; creates AgentRun, dispatches to aiq-server
- `GET /aiq/jobs/{agent_run_id}` — status, events, partial outputs
- `GET /aiq/jobs/{agent_run_id}/stream` — SSE proxy to AIQ's event stream
- `POST /aiq/jobs/{agent_run_id}/cancel` — cancel; ledgered

### Synthesize

- `POST /synthesize` — body `{topic, allowed_connectors[], depth="shallow"|"deep"}`; convenience wrapper around `/aiq/jobs` + synthesis routing
- `GET /synthesis-proposals` — list; filterable by status
- `GET /synthesis-proposals/{id}` — detail with the proposed page + diff against any prior revision
- `POST /synthesis-proposals/{id}/approve` — owner/editor; flips page status to `approved`, records `ApprovalDecision` (created in Inc 5; for Inc 3, a minimal `ApprovalDecision` table is created here and reused later)
- `POST /synthesis-proposals/{id}/reject` — owner/editor; body `{reason}`; soft-deletes draft page; writes `RejectionFeedback`

### Approval (skeleton, expanded in Inc 5)

```python
class ApprovalDecision(CommonColumns, Base):
    __tablename__ = "approval_decisions"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # synthesis_proposal | wiki_revision | review_finding (Inc 5+)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    # approved | rejected
    reason: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    decided_by: Mapped[UUID] = mapped_column(nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

`ApprovalDecision` lands here so Inc 3's approve/reject can record durably; Inc 5 extends usage to reviewer findings.

---

## 3.12 Frontend — chat extensions

The chat composer (Inc 2) gains:

- **`SynthesizeButton`** — inline next to the send button when the prior assistant message had `coverage_judgment="synthesis_needed"`. Click → modal: topic (prefilled), allowed connectors (checkboxes from project's `ConnectorBinding`s), depth (shallow|deep, default deep). Submit → triggers `/synthesize`.
- **Slash command `/synthesize <topic>`** — same modal but user-initiated.
- **`SynthesisProgressCard`** — appears as a non-A2UI inline card while the AIQ job runs. Shows: orchestrator/shallow/deep/clarifier phase progress (via SSE from `/aiq/jobs/{run_id}/stream`), connector calls made, sources persisted (clickable to source detail), cost so far.
- **`SynthesisDraftPreview`** — when the synthesis completes, the chat shows: "Synthesized N pages and M revisions. Review and approve?" with link to the synthesis proposals list. A minimal in-chat preview for each new page (title + summary + citations count).
- **Clarifier interrupts** — AIQ Clarifier surfaces clarification questions back into chat; the user answers inline; the answer is sent back to AIQ via `/aiq/jobs/{id}/clarify`.

---

## 3.13 Tests

### Unit

- `aleph-aiq/tests/test_config_generator.py` — given a ModelProfile + ConnectorBindings, emitted YAML is valid and parses with AIQ's loader
- `aleph-aiq/tests/test_tokenomics_adapter.py` — AIQ PhaseStats → ModelCall + CostLedgerEvent
- `aleph-aiq/tests/test_auth_bridge.py` — service token issuance, callback verification, expiry, scope enforcement
- `aleph-connectors/tests/test_credentials.py` — encrypt/decrypt round-trip; libsodium and KMS schemes; no plaintext in ledger
- Each connector has unit tests in its own subdir (search/fetch/normalize round-trip with mock upstream)
- `aleph-wiki/tests/test_citation_verification_node.py` — bad citation triggers retry; second failure marks claim uncited; AIQ-authored bad citation blocks synthesis commit
- `aleph-wiki/tests/test_synthesis_workflow.py` — AIQ report → workflow → draft pages with status="draft" + SynthesisProposal rows

### Integration (`tests/e2e/`)

- `test_aiq_server_health.py` — aiq-server comes up; `/aiq/health` returns 200; AIQ config loads without errors
- `test_synthesize_deep.py` — POST /synthesize with a topic; verify AgentRun created, AIQ job dispatched, connectors called (mock fixtures for Tavily + arXiv), Sources persisted, synthesis proposals created with status="draft", cost ledger has AIQ-phase entries
- `test_synthesize_approve.py` — Approve proposal → page status flips to "approved" → assistant chat now retrieves the page in subsequent queries
- `test_synthesize_reject.py` — Reject with reason → page soft-deleted → RejectionFeedback row created → next /synthesize with same topic includes the reason
- `test_aiq_writes_through_aleph.py` — Critically: verify AIQ never writes to Postgres directly. Inspect AIQ container's env: no DATABASE_URL. Inspect ledger after a deep research run: every Source has `created_by` = the aleph_agent token's user_id, every ModelCall has `actor_kind="aiq_agent"`.
- `test_aiq_no_s3_creds.py` — Verify AIQ container has no MinIO/S3 credentials; all asset writes happen via the callback.
- `test_connector_credential_isolation.py` — Project A's Tavily key cannot be read by Project B's AIQ run.
- `test_aiq_clarifier_loop.py` — AIQ Clarifier asks a question; UI surfaces it; user answers; AIQ continues.
- `test_disallowed_connector.py` — Project allowlists only Tavily; /synthesize attempts arXiv; AIQ refuses visibly (returns a clear error surfaced in chat); no Source created.

### Eval (`packages/aleph-evals/datasets/inc3_synthesize/`)

- `synthesis_coverage.jsonl` — `{topic, expected_concepts: [...]}`. Run /synthesize, approve, assert WikiIndex has the expected concepts.
- `citation_verification_recall.jsonl` — synthesis with deliberately broken citations in the AIQ report (mocked); assert citation_verification catches them.
- `connector_routing.jsonl` — `{topic, expected_connector_calls: [...]}`. Run shallow synthesis with full connector allowlist; assert AIQ used the right connectors.

CI runs all three under both profiles. `citation_verification_recall` must hit 100% (any miss is a regression).

---

## 3.14 Documentation

- `docs/agents/research-agent.md` — AIQ integration overview, orchestrator/shallow/deep/clarifier, when each is invoked
- `docs/agents/aiq-config.md` — config generator, per-project config, LLM bindings via LiteLLM
- `docs/agents/synthesize-action.md` — the /synthesize flow end-to-end
- `docs/connectors/<each-connector>.md` — one doc per connector with: contract, metadata schema, rate limits, search semantics
- `docs/connectors/connector-contract.md` — the ConnectorBase Protocol, registration pattern, callback contract
- `docs/security/connector-credentials.md` — encryption scheme, rotation
- `docs/security/aiq-boundary.md` — service token, callback boundary, "AIQ has no DB/S3 creds" rule
- `docs/operations/aiq-runbook.md` — start/stop/diagnose aiq-server
- `docs/implementation-log.md` — Inc 3 entry

---

## 3.15 Acceptance criteria

1. **AIQ alive.** `aiq-server` comes up in compose; `GET /aiq/health` → 200; config is generated per-project from ModelProfile + ConnectorBindings.
2. **Synthesize end-to-end.** A `coverage_judgment="synthesis_needed"` from chat → `/synthesize` → AIQ DeepResearcher runs → connectors called → Sources persisted via callback → synthesis_compose workflow → SynthesisProposal rows + draft pages → owner approval → page becomes retrievable in next assistant turn.
3. **No direct AIQ writes.** AIQ container has no DATABASE_URL or S3 credentials. All Sources, ModelCalls, AgentEvents persist via `/internal/v1/aiq/*` callbacks. Verifiable via container env inspection + ledger actor_kind audit.
4. **Connector roster live.** All Inc 3 connectors (Tavily, Exa, Serper, arXiv, Semantic Scholar, OpenAlex, RSS, HuggingFace Hub) execute searches and persist sources. Lens.org is registered but disabled.
5. **Citation verification active.** A synthesis with a fabricated `[c…]` marker fails the citation_verification node; AIQ retries; second failure surfaces a clear error or marks claims uncited.
6. **Tokenomics adapted.** Every AIQ LLM call shows up in the CostLedger with `actor_kind="aiq_agent"`, correct `phase`, correct token + cost figures, correct cache-savings.
7. **Credential isolation.** Project A's Tavily key cannot be accessed by Project B's AIQ run. Verified by test.
8. **Disallowed connector refused.** AIQ tool call to a not-allowlisted connector raises with a clear surfaced error.
9. **Clarifier loop works.** AIQ Clarifier question surfaces in chat; user answer routes back; AIQ continues.
10. **Eval gates pass.** All Inc 3 eval datasets pass under both profiles. `citation_verification_recall` at 100%.
11. **Permission leakage zero.** Members can't read other projects' synthesis proposals, AIQ jobs, connector creds.
12. **Docs complete.** All Inc 3 docs exist.
13. **No placeholders.** Same rule.
14. **Implementation log written.**

---

## 3.16 Handoff to Increment 4

Inc 4 wires A2UI: surface components for each right-panel tab and inline cards used in chat. The BriefsSurface will replace the "synthesis proposals list" of Inc 3 with a proper A2UI-rendered list with `ApprovalCard`s. The synthesis approval API endpoints from Inc 3 don't change — Inc 4 just renders them more nicely.

Inc 4 reuses:
- `SynthesisProposal`, `ApprovalDecision` (just rendered via A2UI)
- The whole chat surface (Inc 2) gains the ability to render `attached_cards_jsonb` via the A2UI inline renderer
- The clarifier loop becomes a `FormCard` (instead of plain chat text)

No schema changes to Inc 3 entities anticipated.

See `docs/superpowers/specs/2026-05-27-inc-4-a2ui-surfaces-design.md`.
