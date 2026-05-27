# Increment 2 — Wiki-First Chat + Assistant

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md` (top-level)
**Depends on:** Inc 0, Inc 1
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 2.1 Scope

Increment 2 makes the wiki *queryable by the analyst via natural language*. The wiki skeleton (Inc 1) is now the substrate; this increment layers the **wiki-first retrieval router** (LLM-routed page-selection → 1-hop wikilink expansion → answer composer) plus the intra-source descent path on top. End of this increment: drop a PDF, watch it become wiki, then chat with the assistant and get cited answers anchored on the wiki — with descent into source chunks when needed.

**This is the moment Aleph becomes Aleph.** Wiki-first chat is the load-bearing differentiator; everything after Inc 2 enriches it.

### In scope

- Models: `AssistantSession`, `AssistantThread`, `AssistantMessage`
- Wiki retrieval router (page selector + 1-hop expansion + answer composer)
- Intra-source descent (when assistant flags coverage gap that a cited `SourcePage` can fill)
- Coverage-gap detection (recognize when descent isn't enough → flag for `--synthesize`; the `--synthesize` action itself lands in Inc 3)
- Assistant agent (LangGraph workflow) wrapping the retrieval router
- HTTP API for sessions, threads, messages, retrieval debug
- Center-panel chat UI with citation hover previews
- Cost badge per message + budget banner enforcement in chat
- Tests + docs + eval datasets for retrieval quality

### Explicitly out of scope

- AIQ research / `--synthesize` action → Inc 3
- Other connectors → Inc 3
- A2UI surfaces → Inc 4
- Reviewer agents → Inc 5
- Datasets, charts → Inc 6
- Builder, artifacts → Inc 7

### Dependencies on prior increments

- Inc 0: `LiteLLMClient`, `LedgerWriter`, `Principal`, `agent_tokens`, OTEL+Langfuse context, `ModelProfile`, `Budget`
- Inc 1: `WikiPage`, `WikiRevision`, `WikiSection`, `WikiLink`, `WikiClaim`, `Citation`, `SourcePage`, `WikiIndex`, `IndexService.select_pages`, `DocumentChunk`, `POST /sources/{id}/chunks/search`

### What downstream increments rely on

- Inc 3: `--synthesize` triggers the AIQ DeepResearcher when the assistant's coverage check fails. The trigger contract is defined here; the AIQ wiring lands there.
- Inc 4: chat-inline A2UI cards (`ApprovalCard`, `ChartCard`) render alongside assistant prose. Inc 2's message model already carries an `attached_cards_jsonb` slot.
- Inc 5: reviewer findings + approvals can be referenced from chat by `[[Finding:F12]]` wikilinks once those exist.

---

## 2.2 Repository changes

```
packages/
└── aleph-assistant/                    # new package
    └── src/aleph_assistant/
        ├── __init__.py
        ├── models.py                   # AssistantSession, AssistantThread, AssistantMessage
        ├── thread_service.py
        ├── retrieval/
        │   ├── __init__.py
        │   ├── router.py               # the wiki-first retrieval router
        │   ├── page_selector.py        # LLM call (capability=page_selection)
        │   ├── expander.py             # 1-hop wikilink expansion
        │   ├── descent.py              # intra-source chunk retrieval (calls Inc 1 endpoint via service)
        │   └── coverage.py             # coverage-gap detection
        ├── composer/
        │   ├── __init__.py
        │   └── composer.py             # answer composer (capability=synthesis)
        ├── agent/
        │   ├── workflow.py             # LangGraph DAG
        │   └── prompts/
        │       ├── page_selector.md
        │       ├── composer.md
        │       └── coverage_judge.md
        └── streaming.py                # SSE/AG-UI bridge for streaming responses

apps/api/src/aleph_api/routes/
├── sessions.py                          # CRUD
├── threads.py                           # CRUD + message append
├── messages.py                          # SSE stream for an assistant message
└── retrieval_debug.py                   # owner-only inspection

apps/web/src/
├── routes/project/$id/session.$sessionId.tsx   # the chat surface in center panel
└── components/
    ├── ChatMessage.tsx
    ├── ChatComposer.tsx
    ├── ChatCitationHover.tsx
    ├── WikilinkHover.tsx
    ├── CostBadge.tsx
    └── BudgetBanner.tsx
```

No new heavy deps. Uses existing LangGraph from Inc 1.

---

## 2.3 Domain model

```python
# packages/aleph-assistant/src/aleph_assistant/models.py

class AssistantSession(CommonColumns, Base):
    """A UI grouping of one or more threads. Each session shows in the left panel."""
    __tablename__ = "assistant_sessions"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # short, auto-generated from the first user message; user-editable
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class AssistantThread(CommonColumns, Base):
    """One conversation. A session usually has one thread; multiple is allowed."""
    __tablename__ = "assistant_threads"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    session_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    parent_thread_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # for forks (Inc 2 supports rerunning a turn — creates a sibling thread)
    title: Mapped[str | None] = mapped_column(String(255))

class AssistantMessage(CommonColumns, Base):
    __tablename__ = "assistant_messages"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    thread_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # user | assistant | system
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    # contains [[wikilink]] and [c12] markers
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    # streaming | complete | failed | budget_blocked
    retrieval_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # snapshot of the retrieval used to compose: page_ids, descent_chunk_ids,
    # coverage_judgment ("ok" | "descent_needed" | "synthesis_needed")
    attached_cards_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Inc 2 = empty list. Inc 4+ populates with A2UI card descriptors.
    agent_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    __table_args__ = (UniqueConstraint("thread_id", "ordinal"),)
```

Migration `<timestamp>_inc2_assistant.py` creates these three tables.

No changes to Inc 1 entities. **Confirmed by `alembic check`.**

---

## 2.4 Retrieval flow (the load-bearing pipeline)

This implements §4.2 of the top-level spec.

```python
# packages/aleph-assistant/src/aleph_assistant/retrieval/router.py

@dataclass
class RetrievalResult:
    selected_pages: list[SelectedPage]      # from page_selector
    expanded_pages: list[ExpandedPage]      # 1-hop wikilink expansion
    descent_chunks: list[DescentChunk]      # only populated if descent ran
    coverage_judgment: Literal["ok", "descent_needed", "synthesis_needed", "descent_used"]
    page_selection_trace: PageSelectionTrace
    descent_trace: DescentTrace | None

class WikiFirstRetrievalRouter:
    async def retrieve(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        thread_id: UUID,
        query: str,
        prior_messages: list[AssistantMessage],   # used for query rewriting
        profile: ModelProfile,
        agent_run_id: UUID,
        top_k_pages: int = 8,
        descent_budget_chunks: int = 12,
    ) -> RetrievalResult:
        ...
```

### Step 1 — page selection

Two sub-steps:

**1a. Candidate generation (deterministic).** Call `IndexService.select_pages(project_id, query, top_k=top_k_pages * 3)` — Postgres FTS over `WikiIndex.index_tsv`. Returns top 24 candidate `(page_id, title, summary, score, wikilinks_out)` rows.

**1b. LLM page-selector.** Call `LiteLLMClient.chat(capability="page_selection", purpose="assistant.page_selection")` with:
- the user's query (possibly rewritten using prior_messages context)
- the candidate list (title + summary + first 5 wikilinks_out)
- the system prompt at `prompts/page_selector.md`
- structured output: `PageSelectionResponse(selected: list[SelectedPage], reason: str)` where each `SelectedPage` has `{page_id, relevance_label}` and `relevance_label ∈ {primary, supporting, peripheral}`

Output: up to `top_k_pages` selected pages, each tagged.

### Step 2 — 1-hop wikilink expansion

For each `primary` page in the selection, follow outgoing `WikiLink` rows with `dst_page_id IS NOT NULL`. Add the top N destinations (by `WikiLink.occurrences`) to the context.

Bounded: at most `top_k_pages` extra pages from expansion. `peripheral`-tagged primary pages don't expand. Expansion is deterministic — no LLM call.

### Step 3 — answer composer

Call `LiteLLMClient.chat(capability="synthesis", purpose="assistant.compose")` with:
- the user's query
- the message history (last N turns, token-budgeted)
- the **selected pages' body_md** (full text — they're already curated; this is the wiki-first design's payoff)
- the expanded pages' body_md (1-hop)
- the system prompt at `prompts/composer.md` (instructs: "answer from the provided wiki pages; preserve `[[wikilinks]]` and `[c12]` citation markers; if a needed fact is missing from the provided pages but a cited `[[Source:X]]` is referenced, output a `<descent-request>` tag with the source short_id and what you need from it; if no source can fill the gap, output a `<synthesis-request>` tag describing what new wiki content is needed.")
- structured output: `ComposerResponse(body_md, descent_requests: list[DescentRequest], synthesis_requests: list[SynthesisRequest])`

### Step 4 — coverage check + descent

If `composer.descent_requests` is non-empty:

For each request `{source_short_id, query_within_source}`:
1. Look up the `Source` by short_id (project-scoped).
2. Call `chunks_service.search_within_source(source_id, query_within_source, top_k=4)` (the Inc 1 endpoint, now invoked as a service method).
3. Collect up to `descent_budget_chunks` chunks total across all descent requests.

Then **re-call the composer** with the descent chunks added to the context. Mark `coverage_judgment = "descent_used"`.

The descent loop runs at most once per turn (no cascading descents in Inc 2; can revisit if eval signal shows it's needed).

### Step 5 — synthesis flag (no action in Inc 2)

If `composer.synthesis_requests` is non-empty after descent (or before, if no descent was applicable), the router:
- sets `coverage_judgment = "synthesis_needed"`
- records the requests in `AssistantMessage.retrieval_jsonb` under `synthesis_requests`
- **does not act on them in Inc 2.** The assistant says (composer output): "I don't see this in the wiki. To answer this I'd need to research and synthesize a new wiki entry. (Run `/synthesize` to start.)" In Inc 2 the `/synthesize` slash command is **not yet** wired; in Inc 3 it will trigger AIQ. For Inc 2, the response is honest about the gap.

If `composer.descent_requests` is empty and no synthesis flag fires, `coverage_judgment = "ok"`.

### Token accounting

Composer context is sized to fit within the binding's `max_input_tokens` minus a generous output reservation. If selected+expanded pages overflow, drop `peripheral`-tagged pages first, then truncate the longest pages with a "[truncated]" marker. Truncation is recorded in `retrieval_jsonb.truncated_pages`.

---

## 2.5 Assistant agent (LangGraph)

```python
# packages/aleph-assistant/src/aleph_assistant/agent/workflow.py

class AssistantTurnState(TypedDict):
    agent_run_id: UUID
    project_id: UUID
    thread_id: UUID
    user_message_id: UUID
    profile: ModelProfile
    prior_messages: list[AssistantMessage]
    budget_check: BudgetState

    # progressive
    rewritten_query: str | None
    retrieval: RetrievalResult | None
    composed_body: str | None
    descent_requests: list[DescentRequest] | None
    synthesis_requests: list[SynthesisRequest] | None
    assistant_message_id: UUID | None  # set when row is created (status=streaming)
    cost_usd: Decimal
```

Nodes:

1. **`budget_gate`** — read `Budget.spent_usd`, project's `cap_usd`, soft/hard percentages. If at hard cap, mark message `status="budget_blocked"`, set body to a clear explanation, commit, end workflow.
2. **`query_rewrite`** — short LLM call (`capability="classification"`) only if prior_messages is non-empty. Rewrites the user query to be self-contained ("the one we discussed" → "Transformer Capacity"). Skipped on first turn.
3. **`retrieve`** — runs `WikiFirstRetrievalRouter.retrieve`.
4. **`compose`** — runs composer. Updates message body, status remains `streaming` (composer streams tokens to the SSE channel).
5. **`maybe_descend`** — if `descent_requests`, runs descent + re-compose. Idempotent.
6. **`finalize`** — flip status to `complete`. Persist `retrieval_jsonb`, `cost_usd`, `latency_ms`. Ledger event `assistant.message.complete`.

Per-turn there is exactly one `AgentRun` (kind=`assistant`). Every LLM call is a span; every model call writes `ModelCall` + `CostLedgerEvent`. Span hierarchy makes the per-turn cost rollup query trivial.

Failure handling:
- Composer error → status=`failed`, error_text set, retain partial body if any was streamed, ledger event `assistant.message.failed`. UI shows a retry button (which forks a new sibling thread via `parent_thread_id`).
- Budget exceeded mid-turn (descent puts us over) → finalize message at whatever was streamed; mark `status="budget_blocked"`; clear UI banner.

---

## 2.6 Streaming

The composer is invoked with `stream=True`. The LangGraph node bridges the streaming LiteLLM response to two consumers:

1. **SSE stream** (`GET /v1/projects/{id}/messages/{message_id}/stream`) — server-sent events: `token`, `wikilink_resolved`, `citation_revealed`, `descent_started`, `descent_complete`, `done`.
2. **DB persistence** — periodic flush (every 200 tokens or 1s) updates `AssistantMessage.body_md` incrementally. On stream-end, final write with `status="complete"`.

Frontend subscribes to the SSE channel when a new user message is sent, replays tokens, and shows live citation hover targets as `[c12]` markers stream in (the citation row already exists when the composer emits the marker — the composer is given `Citation` rows of selected pages as part of its context, indexed by marker).

---

## 2.7 HTTP API

All under `/v1/projects/{project_id}/`. Auth + project_scope from Inc 0.

### Sessions

- `POST /sessions` — create empty session; returns `{session_id, thread_id}` (auto-creates initial thread)
- `GET /sessions` — list (left panel populates from this)
- `GET /sessions/{id}` — detail
- `PATCH /sessions/{id}` — title rename
- `DELETE /sessions/{id}` — soft delete; ledgered

### Threads

- `GET /sessions/{id}/threads` — list (multiple if forked)
- `POST /sessions/{id}/threads` — fork (body specifies `parent_thread_id`, `from_ordinal` — copies messages up to that ordinal then drops the rest)

### Messages

- `GET /threads/{id}/messages` — paginated history
- `POST /threads/{id}/messages` — body `{body_md}`; appends user message, enqueues assistant turn job, returns the new user_message + a placeholder `assistant_message_id` for the in-progress response
- `GET /messages/{id}/stream` — SSE stream of an in-progress assistant message
- `GET /messages/{id}` — full message after completion
- `POST /messages/{id}/retry` — fork a new sibling thread from the prior ordinal and re-run; ledgered

### Retrieval debug

- `POST /retrieval/debug` — owner/editor; body `{query, top_k}`; runs retrieval router WITHOUT composing or persisting a message; returns `RetrievalResult`. Used to tune page-selection without burning composer cost.

---

## 2.8 Frontend — center panel chat

The center panel goes from "Chat lands in Increment 2" placeholder to a real chat surface.

### `session.$sessionId.tsx`

- Loads session + initial thread
- Renders message list (oldest top, newest bottom)
- SSE-subscribes to in-progress message
- Composer at bottom with model-profile picker (defaults to project's profile; per-message override allowed; ledgered)
- Each message shows: role badge, body (rendered as Markdown with custom `[[wikilink]]` and `[c12]` extensions), cost badge (assistant only), trace link (owner/editor only)
- Forking: each message has a "retry from here" action (owner/editor)

### `ChatMessage.tsx`

Renders Markdown with two custom extensions:

- **`[[wikilink]]`** → `WikilinkChip` from Inc 1, hover-previews the target page from `WikiIndex` (title + summary), click navigates to wiki tab + that page
- **`[c12]`** → `CitationHover` — hovers reveal the `Citation` target: if `source_page_id`, show source title + URL + retrieval timestamp; if `chunk_ids`, show the chunk excerpt with section_path

### `CostBadge.tsx`

`$0.04 · 1.2s · 2.4k in / 380 out (54% cached)` — clickable to open the trace in Langfuse (owner/editor only).

### `BudgetBanner.tsx`

Top of chat. Three states:

- Green: < soft_pct
- Yellow: between soft_pct and hard_pct ("$42.18 of $50 used. Approaching the cap.")
- Red: ≥ hard_pct ("Hard cap reached. New turns disabled until budget is raised by an owner.")

### Activity card update

The center-panel activity card (built in Inc 0) now subscribes to the SSE channel for the current thread's in-progress message and shows token-flow rate + descent status.

---

## 2.9 Tests

### Unit

- `aleph-assistant/tests/test_page_selector.py` — given a fixture of `WikiIndex` rows and a query, the LLM page selector returns the expected pages (LLM stubbed; tests the prompt assembly + response parsing)
- `aleph-assistant/tests/test_expander.py` — given selected pages with known `WikiLink` rows, expansion picks the right neighbors
- `aleph-assistant/tests/test_composer.py` — given a context bundle, composer prompt is built correctly; response with `descent_requests` is parsed
- `aleph-assistant/tests/test_descent.py` — descent invokes the chunk search service correctly; coverage tag updates
- `aleph-assistant/tests/test_workflow.py` — full turn workflow with mocked LLM calls; verifies all nodes execute, ledger + cost rows written

### Integration (`tests/e2e/`)

- `test_chat_wiki_first.py` — Upload a fixture PDF → wait for wiki_done → POST a user message asking about a concept in the source → verify assistant response: status=complete, contains `[[wikilinks]]` to known pages and `[c12]` markers to known citations, `retrieval_jsonb.selected_pages` matches expectations, `coverage_judgment="ok"`.
- `test_chat_descent.py` — Use a fixture source with a specific detail buried in only one section. Ask about that detail. Verify `coverage_judgment="descent_used"`, `retrieval_jsonb.descent_chunks` contains the right chunk_ids.
- `test_chat_synthesis_flag.py` — Ask a question that no wiki page covers. Verify `coverage_judgment="synthesis_needed"`, assistant body explicitly says synthesis is needed, message lands with status=complete (NOT failed).
- `test_chat_streaming.py` — Subscribe to SSE stream during turn; verify tokens stream and final state matches DB.
- `test_chat_budget_soft.py` — Set tiny budget, exceed soft cap → response includes warning banner; turn completes.
- `test_chat_budget_hard.py` — Exceed hard cap → next turn returns `budget_blocked` status with helpful body; no LLM call made.
- `test_chat_retry_fork.py` — Send turn, retry from prior ordinal; verify new thread row with `parent_thread_id` set, messages up to ordinal copied, new turn runs.
- `test_no_raw_chunk_rag.py` — Ask a question that *could* be answered by a chunk but is also in a wiki page; verify the response cites the wiki page, not the raw chunk. (Negative test for the "wiki is primary" rule.)

### Eval (`packages/aleph-evals/datasets/inc2_retrieval/`)

- `page_selection.jsonl` — `{query, expected_page_slugs: [...]}`. Run page selector. Gate: top-3 contains ≥1 expected; recall@8 ≥ 70%.
- `descent_correctness.jsonl` — `{query, source_short_id, expected_chunk_substrings: [...]}`. Run full turn. Gate: descent chunks contain ≥1 expected substring.
- `citation_correctness.jsonl` — `{query, expected_citation_markers: [...]}`. Run full turn. Gate: every `[c…]` marker in the response resolves to a real `Citation` row.
- `synthesis_flag_precision.jsonl` — `{query, expected_judgment}`. Run full turn. Gate: `coverage_judgment` matches expected for 80% of cases.

CI runs against both ModelProfiles. `synthesis_flag_precision` failure under `aleph-production` blocks deploy.

---

## 2.10 Documentation

- `docs/agents/assistant-agent.md` — workflow, prompts, descent loop, synthesis flag semantics
- `docs/retrieval/wiki-first-router.md` — page-selector design, expansion, descent, token budget, coverage judgments
- `docs/ui/chat-surface.md` — chat UI behavior, citation hovers, cost badge, budget banner, fork-retry
- `docs/implementation-log.md` — Inc 2 entry

---

## 2.11 Acceptance criteria

1. **Wiki-first answer.** Upload a PDF → wait for wiki_done → POST a question → assistant response cites wiki pages with `[[wikilinks]]` and `[c12]` markers, `coverage_judgment="ok"`, no chunk embeddings consulted (verifiable in `retrieval_jsonb`).
2. **Descent works.** Question requiring detail not in wiki summary → `coverage_judgment="descent_used"`, descent chunks present, answer includes the detail with a `[c…]` citation back to the source chunk.
3. **Synthesis flag honest.** Question with no wiki coverage → response explicitly says synthesis is needed (the `/synthesize` action doesn't fire yet — Inc 3).
4. **Streaming works.** SSE channel emits incremental tokens; UI replays tokens; final state in DB matches.
5. **Cost tracked.** Every turn produces `ModelCall` + `CostLedgerEvent` rows. Cost badge on the message matches the sum.
6. **Budget enforcement.** Soft cap → banner yellow, turns continue. Hard cap → turn returns `budget_blocked` with no LLM call made.
7. **Fork retry.** Retry on prior message produces a new sibling thread, ledgered.
8. **Permission leakage zero.** Member of Project X can not list/read sessions or threads or messages of Project Y. 404.
9. **Eval gates pass.** `page_selection`, `descent_correctness`, `citation_correctness`, `synthesis_flag_precision` all pass under both profiles.
10. **No RAG-first fallback.** Static analysis + test verifies: the assistant code path never calls `chunks_service.search_within_source` *unless* `coverage_judgment != "ok"` and `composer.descent_requests` is non-empty. No "embedding fallback if wiki retrieval is weak."
11. **Docs complete.** Files in §2.10 exist.
12. **No placeholders.** Same rule.
13. **Implementation log written.**

---

## 2.12 Handoff to Increment 3

Inc 3 wires the `--synthesize` action (currently a flag with no action) to AIQ DeepResearcher, brings in the full connector roster, and lets the wiki grow from real queries.

Inc 3 reuses:
- `RetrievalResult.synthesis_requests` (the trigger contract is already shaped)
- `WikiFirstRetrievalRouter` (no change; descent now might land on connector-fetched sources)
- The `AssistantMessage.attached_cards_jsonb` slot (still empty; Inc 4 fills it; Inc 3 may use it for "synthesis kicked off" status cards)
- Cost ledger + budget (AIQ tokenomics adapter feeds the same ledger)

See `docs/superpowers/specs/2026-05-27-inc-3-aiq-connectors-synthesize-design.md`.
