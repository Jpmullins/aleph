# Per-increment acceptance tests

Each Aleph increment ships against a workflow-level acceptance gate, not just unit tests. This doc records the gate per increment — what a passing run actually exercises. The Playwright e2e suite under `tests/playwright/` is the canonical implementation of these gates for the user-facing increments.

## Inc 0 — Foundations + LiteLLM transport

**Gate:**
- `uv sync --all-packages` resolves cleanly.
- `docker compose up` brings the full stack to `/readyz` green (all 4 checks: postgres, redis, minio, litellm_gateway).
- A single end-to-end `smoke_llm` call routes through the LiteLLM gateway and writes one `ModelCall` + one `CostLedgerEvent` row.
- An action ledger event is written for every state mutation (assert via integration test).

**Where:** `tests/e2e/test_smoke_llm.py`, `tests/e2e/test_ledger_immutable.py`, plus health-check assertions on `/readyz`.

## Inc 1 — RKS + wiki schema + upload + normalization

**Gate:**
- Upload a markdown / PDF source via `POST /v1/projects/{id}/sources/upload`.
- `normalize_job` AgentRun transitions `pending → running → succeeded` (visible in `/agent-runs`).
- `chunk_embed_job` runs to `succeeded`; `DocumentChunk` rows exist with non-null embeddings.
- `wiki_ingest_job` runs to `succeeded`; at least one `WikiRevision` exists for the project.
- Hand-edit applied to a wiki page is preserved across a re-ingest of the same source.
- Rejecting a draft section records a `RejectionFeedback` row that is included in the next compile prompt.

**Where:** Playwright `03-source-to-wiki.spec.ts`. Integration tests for the hand-edit and rejection-feedback paths live in `packages/aleph-wiki/tests/`.

## Inc 2 — Wiki-first retrieval + assistant chat

**Gate:**
- Send a chat message; an `AssistantMessage(role=assistant)` appears with `retrieval_jsonb.coverage_judgment` set.
- The page-selector LLM call resolves to ≥1 selected page when wiki coverage exists.
- Intra-source descent fires when the composer flags missing detail and a `[[Source:X]]` is cited.
- `assistant_turn` AgentRun lifecycle complete (`succeeded` on every happy turn).
- `[[Wikilink]]` and `[c12]` markers in answers hover-preview the page / chunk in the UI.

**Where:** Playwright `03-source-to-wiki.spec.ts` (assistant section), `02-workspace-shell.spec.ts` (Enter to send, Shift+Enter newline).

## Inc 3 — AIQ + connector roster + `/synthesize`

**Gate:**
- `/v1/projects/{id}/synthesize` dispatches an AIQ deep-research job (`AgentRun(agent_kind="aiq_deep")`).
- AIQ tool callbacks succeed against `/internal/v1/aiq/*` using `X-Aleph-Service-Token`.
- A `SynthesisProposal` row lands in Briefs as an `ApprovalCard`; approving it commits a new wiki revision.
- All 9 connectors register (Tavily, Exa, Serper, arXiv, Semantic Scholar, OpenAlex, Lens, RSS, HuggingFace Hub) in the AIQ data-source registry.
- AIQ container reachable at `http://aiq-server:8000/health`.

**Where:** Playwright `04-right-panel-surfaces.spec.ts` Briefs flow (approval round-trip). Connector registration verified via `GET /v1/connectors`.

## Inc 4 — A2UI catalog + 5 surfaces + 12 inline cards

**Gate:**
- `GET /v1/a2ui/catalog` returns 5 surfaces + 12 inline card types.
- Each of the 5 tabs (Wiki/Artifacts/Notes/Hypotheses/Briefs) renders without console errors.
- Backend surface composer emits real components, not stubs:
  - Wiki: `ClaimCard`s + `SourceCard`s for the project's claims/sources.
  - Notes: `NotebookCellCard`s for project notes.
  - Briefs: `ApprovalCard`s for pending synthesis proposals + `FindingCard`s for review findings.
- ActionRouter handles all 10 action kinds (approve/reject/open/navigate_wiki/submit_form/create_hypothesis/edit_note/clarify/mark_handedit/clear_handedit).

**Where:** Playwright `04-right-panel-surfaces.spec.ts` (per-tab no-error check + Hypotheses + Artifacts flows).

## Inc 5 — Reviewers + Approval + Hypotheses + AgentMemory

**Gate:**
- After any `wiki_service.commit_revision()` succeeds, a `mechanical_review_job` AgentRun appears within 10s (auto-enqueue).
- `MechanicalReviewer` produces ≥1 `ReviewFinding` for a synthetic broken-citation fixture revision.
- `EditorialReviewer` runs on schedule or threshold; HITL ApprovalCard appears.
- Hypothesis create/update flow: title + statement + initial confidence; evidence addition shifts confidence.
- Rejection of a draft section produces a `RejectionFeedback` row.

**Where:** Playwright `04-right-panel-surfaces.spec.ts` Hypotheses + feedback button. Reviewer auto-enqueue verified via `03-source-to-wiki.spec.ts` (`waitForAgentRun(... "mechanical_review")`).

## Inc 6 — Datasets + visualization cards

**Gate:**
- artificialanalysis.ai connector ingests rows → `Dataset` + `DatasetVersion` + `Observation` rows.
- A `ChartCard` bound to a `DatasetVersion` renders a real Vega-Lite canvas.
- A `TableCard` is sortable and filterable.
- A `MapCard` mounts a MapLibre canvas.
- A `GraphCard` renders nodes + edges SVG.

**Where:** Playwright `05-charts-tables-graphs.spec.ts`.

## Inc 7 — Builder + RenderedAssets + Artifacts + export

**Gate:**
- POST `/v1/projects/{id}/artifacts/build` dispatches `builder_job` (AgentRun lifecycle complete).
- ArtifactsSurface shows the artifact + a working download link when status="ready".
- Builder output includes a cited bibliography (CSL formatter) when wiki has cited claims.
- DOCX / markdown-bundle export available.

**Where:** Playwright `04-right-panel-surfaces.spec.ts` Artifacts build flow.

## Inc 8 — Evals + UserFeedback + regression gates

**Gate:**
- FeedbackButton on `ClaimCard` / `SourceCard` / `ChartCard` / `HypothesisCard` / `FindingCard` POSTs to `/v1/projects/{id}/feedback`.
- `marked_wrong` / `misleading` / `false_positive` signal auto-promotes a `UserFeedback` row to an `EvalCase` under `user_feedback:{project_id}`.
- `python -m aleph_evals --datasets all --gate strict` runs and returns 0 on a green run.
- Cost-drift detection alarms above 15%.

**Where:** Playwright `04-right-panel-surfaces.spec.ts` (Feedback button flow). CI gate via `.github/workflows/ci.yml` evals job.

## How to run

```bash
# One-time
(cd tests/playwright && pnpm install && pnpm exec playwright install --with-deps chromium)

# Full suite against a running stack
(cd tests/playwright && pnpm test)

# Single spec
(cd tests/playwright && pnpm exec playwright test 02-workspace-shell.spec.ts)

# Headed (for debugging)
(cd tests/playwright && pnpm test:headed)
```

The suite assumes the full compose stack is up and the local-mode auth is on (`ALEPH_AUTH_MODE=local`). Override the targets with `ALEPH_WEB_BASE_URL` and `ALEPH_API_BASE_URL` if running against a non-default deployment.
