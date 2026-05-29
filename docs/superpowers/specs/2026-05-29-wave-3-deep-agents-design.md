# Wave 3 — Deep Agents harness + subagents + SKILL.md (design)

Status: **planned** (not started). Author handoff doc — read the
"References" section first; it lists exactly which local repos, docs, MCP
servers, and skills to consult so you plan against *correct, current* APIs.

## Why this wave

Spec §6 (`2026-05-27-inc-5-reviewers-hypotheses-design.md`) says the
EditorialReviewer should run on the **Deep Agents harness**. The
implementation is plain LangGraph. Same for the wiki and assistant agents.

**Honest scoping note (verified 2026-05-29):** the original plan claimed
"none of the agents have subagents." That is **wrong** — they already
decompose their work, just not via the Deep Agents harness:

- `packages/aleph-reviewer/src/aleph_reviewer/editorial/workflow.py` runs 5
  role-scoped passes (`_node_contradiction`, `_node_weak_source`,
  `_node_narrative_gap`, `_node_coverage_gap`, factual-freshness) via a
  homegrown `_run_subagent(name, …)` helper. Its `subagents/` dir is **empty**.
- `packages/aleph-wiki/src/aleph_wiki/agent/` has a `nodes/` breakdown.
- `packages/aleph-assistant/src/aleph_assistant/agent/workflow.py` is a
  single LangGraph (its `_active_ctx` global was already converted to a
  `ContextVar` in W2.1; the synthesis workflow's was converted in the
  research→wiki work — mirror that pattern).

So **W3 is an architecture upgrade, not new capability.** It buys: SKILL.md
progressive disclosure, cross-session memory (`StoreBackend`), parallel
subagent dispatch (`AsyncSubAgentMiddleware`), and harness-managed planning —
not "the agents can suddenly do more." Decide if that's worth it before
starting; it is the lowest-value of the remaining waves.

## What ships

### W3.1 — EditorialReviewer → Deep Agents harness
Replace the LangGraph workflow with `create_deep_agent(...)`:
- `model` = `ModelProfile` `judge` capability via `ChatOpenAI(base_url=gateway)`
  (CLAUDE.md rule #2 — agent-framework code may use ChatOpenAI **only**
  pointed at the Insights gateway).
- `subagents=` for contradiction_finder / source_quality / coverage_gap /
  narrative_critic (mirror the 5 current `_run_subagent` roles).
- `SKILL.md` files at `editorial/skills/<name>/SKILL.md` (YAML frontmatter:
  `name`, `description`, `allowed-tools`; body = procedure). Progressive
  disclosure: the supervisor only loads a skill body when its description
  matches.
- Findings still flow through the existing `review_service.add_finding` /
  `approval_service.create_request` (do NOT let the agent write DB directly —
  rule #3). Integration test must still assert `ReviewFinding` rows + ledger
  events per finding (rule #4).

### W3.2 — Wiki agent supervisor + subagents
`packages/aleph-wiki/src/aleph_wiki/agent/` → supervisor + role subagents
(concept_extractor / alias_extractor / page_composer / claim_extractor /
wikilink_resolver). Parallelize independent extraction (concept + alias +
claim read the same doc) via `AsyncSubAgentMiddleware`; keep wikilink-resolve
sequential after compose. Path-scoped write authority per role
(open-analyst's `WriteAuthorityMiddleware` pattern — see References).

### W3.3 — Assistant supervisor + subagents
The Live assistant (`apps/api/src/aleph_api/copilot_agent.py`, built with
`deepagents.create_deep_agent` already) gains subagents: retriever
(page-selector + 1-hop wikilink expansion), descent (intra-source chunk
search), composer (cited answer), synthesizer (coverage-gap → calls the
`start_research` path that already exists). The assistant is already a Deep
Agent, so this is additive `subagents=` config, not a rewrite.

### W3.4 — Memory backend
Postgres `StoreBackend` keyed per `project × agent`. The `AgentMemory` model
in `aleph-db` is the persistence target. Editorial reviewer stores past
rejection patterns; wiki composer stores style observed from analyst
hand-edits (the rejection-feedback / hand-edit patterns come from
`~/code/obsidian-llm-wiki-local`).

### W3.5 — Verification
- Per-subagent unit tests (mock each SKILL.md).
- `tests/playwright/specs/08-deep-agents.spec.ts`: editorial review on a
  fixture page with a known contradiction → assert ≥1 `ReviewFinding`.
- All prior waves' Playwright specs still pass.

## References — consult these for correct, current APIs

**Deep Agents (the critical one):** `deepagents` is pinned at **0.6.6** in
this repo (`apps/api/pyproject.toml`). Verify the API before coding — it
moves fast. Exported names confirmed present in 0.6.6:
`create_deep_agent`, `SubAgent`, `SubAgentMiddleware`,
`AsyncSubAgentMiddleware`, `AsyncSubAgent`, `CompiledSubAgent`,
`FilesystemMiddleware`, `MemoryMiddleware`, `RubricMiddleware`,
`StateBackend`/`StoreBackend` (via `backends`), `HarnessProfile`,
`register_harness_profile`. Probe with
`uv run python -c "import deepagents; print(dir(deepagents))"`.

- **Skills (preferred docs source):** the `Skill` tool exposes
  `deep-agents-core`, `deep-agents-orchestration`, `deep-agents-memory`,
  `framework-selection`, `langgraph-fundamentals`, `langgraph-persistence`,
  `langchain-middleware`, `langchain-dependencies`. **Invoke
  `framework-selection` first, then `deep-agents-core` + `deep-agents-orchestration`
  + `deep-agents-memory`** — they document `create_deep_agent()`, SKILL.md
  format, `SubAgentMiddleware`, and `StoreBackend` against the installed
  version. These are the authoritative source for this wave.
- **MCP — `docs-langchain`** (`mcp__docs-langchain__search_docs_by_lang_chain`,
  `query_docs_filesystem_docs_by_lang_chain`): LangGraph/LangChain/Deep Agents
  official docs. Use for StateGraph/middleware specifics.
- **Local repo — `~/code/ARLIS/open-analyst`** (`services/langgraph-runtime/`):
  the practical reference for supervisor + role-scoped subagents,
  `WriteAuthorityMiddleware`, and `AsyncSubAgentMiddleware` usage. The plan's
  W3.2/W3.3 patterns are ported from here.
- **Local repo — `~/code/obsidian-llm-wiki-local`**: rejection-feedback,
  hand-edit, and alias patterns that feed the memory backend (W3.4).
- **CLAUDE.md rules** #2 (gateway-only ChatOpenAI), #3 (agent→service only),
  #4 (ledger event per mutation), #5 (ModelCall+CostLedgerEvent — note the
  known gap: ChatOpenAI bypasses `LiteLLMClient`, so a langchain callback
  handler is still the planned fix for cost attribution), #7 (Capability→model).
- **Existing in-repo pattern to copy:** the assistant Deep Agent in
  `apps/api/src/aleph_api/copilot_agent.py` (W2) — `create_deep_agent(model=
  ChatOpenAI(base_url=gateway), tools=[…], middleware=[CopilotKitMiddleware()],
  checkpointer=MemorySaver())`. W3 extends this exact graph with `subagents=`.
- **Gotcha:** `langgraph` is pinned `>=1.2,<2` (resolved 1.2.2); `langgraph
  ==1.0.6` broke `deepagents` prebuilt imports (`ExecutionInfo`). Keep the
  range.

## Out of scope
A langchain callback handler to close the rule-#5 cost-attribution gap for
ChatOpenAI traffic (tracked separately; affects all agent-framework LLM calls,
not just W3).
