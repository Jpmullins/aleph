# Aleph — System Assessment (2026-06-30, post audit-remediation)

> Supersedes the 2026-05-29/30 assessment. Written after the `audit-remediation`
> branch (~35 commits) closed the bulk of the 2026-06-29 audit's 34 findings, and
> after a **fresh four-reviewer re-review** of the remediated tree (which itself
> caught and fixed two critical self-inflicted regressions — see §4). Method:
> file:line verification + the full gate suite + live in-browser checks.

## What the codebase is supposed to do

A multi-agent research environment: **RKS** (ingest → normalize → chunk → embed raw
sources), a **compiled wiki** as the primary retrieval surface (wiki-first, not
RAG-first), and a **conversational A2UI workspace** where a Deep-Agents orchestrator
delegates to 6 subagents and renders results as declarative cards across right-panel
tabs. Eight load-bearing rules (ledger per mutation, cost per LLM call, gateway-only
LLM, agent→service only, project_id everywhere, ModelProfile resolution, declarative
A2UI, wiki-first retrieval).

## What it actually does now (verified)

The core loop is real and works end-to-end:

- **RKS** ingests raw bytes for all three paths (upload, URL, AIQ-captured) into
  MinIO, normalizes, chunks, and embeds into pgvector. Sources are now **visible +
  viewable** in the renamed **Library** tab (Raw asset via a browser-reachable
  presigned URL; Text via normalized markdown) — verified live in-browser.
- **Wiki-first retrieval** routes FTS page-selector → 1-hop expansion → composer,
  embedding descent intra-source-only. The retriever subagent wraps it. (The
  re-review caught that the remediation had deleted its prompt files; restored.)
- **The curator** now knits the graph on **every** authoring path: repair broken
  links, register aliases, **cross-link siblings from prose**, recurate the overview,
  detect near-duplicates → human-gated merge ApprovalCards (Briefs) → `apply_merge`
  that rewrites inbound bodies. Visibly producing interlinked topic pages live.
- **The conversational orchestrator + 6 subagents** resolve their model from the
  project's ModelProfile per capability (Opus under `aleph-production`), with
  server-enforced ApprovalCard gating and per-turn cost rows.
- **Ledger** covers every mutation (the four prior holes — alias upsert/repair,
  hand-edit, feedback, plus merge-propose — now write events) and is **runtime-
  verifiable** (`GET …/ledger/verify`).

## Quality gates (current, hard numbers)

| Gate | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean |
| `pyright` (strict) | 0 errors (warnings remain as tracked debt) |
| `pytest -m "not integration"` | **166 passed** |
| `pytest -m integration` (live stack) | **55 passed, 0 failed** |
| web `tsc` / ESLint / build | clean (chunk-size advisory only) |
| `alembic check` | no drift |

## Remediation scorecard (the 2026-06-29 audit's 34 findings)

**Closed (≈26):** F03/F04/F05/F22/F23/F24 (curator), F06/F07/F10 (agent rules),
F08/F09 (ledger holes + chain verify), F11-partial, F12/F16/F20/F28/F29/F30
(dead-code + drift), F17/F18/F33 (re-embed + profile switch), F31/F32 (retrieval
quality), plus the **raw-source Library feature** the user asked for.

**Honest open items (genuinely remaining):**

1. **Connectors (F01/F02) — research is still effectively Tavily-only.** The
   `aleph-connectors` `ConnectorBase` suite is still orphaned; `dispatch_deep` still
   drops `data_sources`; `emit_config` still has no production caller. Making
   arxiv/semantic_scholar/etc. drive research needs a **custom AIQ image** with those
   sources as NAT plugins (the user's chosen Option B) — a real Docker/plugin build,
   deferred as infra. The feasible interim (submit-time `data_sources` filter over
   AIQ's built-in tavily/exa/scholar) is specced but unbuilt.
2. **Playwright render worker (F-render) — not built.** URL ingest is raw-HTTP, which
   captures static pages fine but not JS/SPA-rendered DOM. CLAUDE.md's "Playwright
   render" claim is aspirational (corrected). The Library viewer renders whatever was
   captured.
3. **Re-embed is reachable but rarely triggered (F17 nuance).** Both seeded profiles
   share `titan-embed-v2`, so a named-profile switch never changes the embedder; the
   per-capability PATCH route now also enqueues reembed on embed change (fixed), but
   there's a latent fixed-`Vector(1024)` hazard if a non-1024 embedder is ever set.
4. **Dormant A2UI surface plumbing (F13/F14/F15/F19) — unchanged.** NotebookCellCard
   children, ClaimCard, wiki embeds, and `ArtifactCard` are built backend-side but
   the self-fetching React surfaces don't render surface children, so they stay dead.
   The Library uses its own rows, not `ArtifactCard`. Not bugs — dead weight.
5. **A2UI delta substrate (F11)** still dormant for tabs other than the intended
   Hypotheses wiring (the Hypotheses delta wiring itself is the one remaining
   "wire it" task).
6. **cross_link aggressiveness:** links any title/alias ≥4 chars; common-word titles
   could over-link. Markdown-link/URL/code spans are now protected; first-occurrence
   only; idempotent.

## Bottom line

The spine is genuinely solid and now substantially more honest than the May
assessment claimed: the ledger/cost/profile rules actually hold on the conversational
surface, the curator knits the graph, and raw sources are visible and viewable. The
re-review's value was concrete — it caught that the legacy-pipeline deletion had taken
two load-bearing retrieval prompts with it (primary grounding would have crashed) and
that curator recommits were dropping page provenance; both are fixed and now
regression-tested. The largest remaining gap is **multi-source research connectors**,
which is real infra (a custom AIQ image), honestly deferred rather than faked.
