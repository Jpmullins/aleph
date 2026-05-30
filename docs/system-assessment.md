# Aleph — System Assessment (2026-05-29)

> **UPDATE 2026-05-30 (Realtime push + Live Wiki wave merged):** every SSE stream
> (agent-events, surfaces, assistant, and a new `changes` stream) now wakes on a Postgres
> **LISTEN/NOTIFY** push (triggers → a supervised `NotifyListener` → in-process `ChangeBroker`
> fan-out) instead of idle polling, each with a self-healing poll fallback. The **wiki tab is
> now live**: index + open page refresh the instant an agent writes (the open page previously
> never refreshed), with "✦ editing…" presence + an "updated" pulse. Verified: 113 unit + 24
> integration tests, a live end-to-end check against the rebuilt running stack, and a
> headless-browser (Playwright) confirmation of the presence lifecycle. **CI green on `main`.**
> This narrows P1 #6 — the SSE×OIDC gap now spans all four streams (still `local`-mode-only;
> documented; the push is server-side so the EventSource auth limitation is unchanged) and
> remains the one item to address before an OIDC deploy. New honest limit: hand-edits write
> no ledger event, so they are not pushed live (the editor sees their own edit locally).
>
> **UPDATE 2026-05-29 (post CI-fix + coverage):** **all five CI jobs are now green on
> `main`** — `lint-and-typecheck`, `unit-tests`, `integration-tests`, `evals`, `build-web`.
> The pipeline itself had been red (every job ran `uv sync --all-extras`, which leaves
> workspace members uninstalled → pyright import failures; `ALEPH_ENV: ci` was invalid;
> integration-tests had no MinIO; the permission test was vacuous under local auth) — all
> fixed (see `implementation-log.md`). P2 #8 (agent-surface test coverage) is **addressed**:
> unit 69→**97**, integration 12→**14**. The remaining open items are now just P1 #6
> (SSE×OIDC — documented, implementation deferred until an OIDC deploy) and P2 #9/#10
> (baked-image dev nits; carried external/op issues).
>
> **UPDATE 2026-05-29 (post green-the-gates):** the P0 hygiene gates below are now
> **GREEN**. `ruff check` 0 errors (was 854), `ruff format` clean (was 125 files),
> `pyright` 0 errors (was 337 — framework/untyped-lib fallout downgraded to *warnings*
> with per-rule justification in `pyproject.toml`; real-bug rules stay errors; 1628
> warnings remain as tracked, visible debt), web **ESLint** now has a flat config and
> lints clean (was broken/no-config), unit tests **69 pass / 0 fail** (the stale eval
> test was fixed). web `tsc`, evals (`pass_rate 1.0`), and `alembic check` were already
> green. The original red-gate analysis is retained below for the record.

A full-system review after Waves 6, 4, and 3 (+ the alembic-drift fix) merged to
`main` (`9eeb041`, pushed to origin). Method: four parallel subsystem reviews
(backend rules, frontend/A2UI, docs accuracy, ops/security/CI) + the full quality
gates run directly. Brutally honest: the **product works and is browser-verified
end-to-end**; at review time the repo's **lint/format/typecheck hygiene gates were red**
— that red was overwhelmingly accumulated cosmetic/framework-untyped noise + tooling-
config gaps, not functional bugs (since remediated — see the UPDATE banner above).

## Executive summary

| Dimension | State |
|---|---|
| Core product (research → wiki → conversational orchestrator + subagents + A2UI) | ✅ Works, verified live in-browser this session |
| 8 load-bearing architecture rules | ✅ Strong adherence; the historical rule-#5 cost gap is **closed** |
| Functional tests (unit pass-rate, evals gate, web tsc/build, alembic) | ✅ Green — all 5 CI jobs pass on `main` (unit 97, integration 14) |
| Code hygiene gates (ruff, ruff-format, pyright, web ESLint) | ✅ Green — cleared in the green-the-gates pass (was red: cosmetic/untyped-framework + missing ESLint config) |
| Production-readiness (OIDC/SSE, deps, test coverage of new agent surface) | ⚠️ One gap left: SSE×OIDC (documented, deferred to an OIDC deploy). Deps cleared; agent-surface coverage added |

## What works (verified this session, in a real browser)

- **Conversational orchestrator + subagents (W3):** a multi-step request made the
  Live assistant delegate via the `task` tool to the **retriever** subagent
  (isolated deep wiki read → distilled cited answer) and the **analyst** subagent
  (created a hypothesis with 4 evidence, rendered in the Hypotheses tab); skills
  ("read the research skill") and per-project memory ("check memories") engaged;
  subagent cost rows confirmed in `model_calls` (`assistant.subagent.retriever`).
- **A2UI v0_9 (W4):** all 5 right-panel tabs render via the shared upstream catalog
  + `MessageProcessor`; a new hypothesis appeared on the Hypotheses tab via an SSE
  **delta** (no reload); the Live chat renders cards from the same catalog.
- **Conversational completion (W6):** Live is the only chat; ApprovalCard gates a
  consequential build (approve → artifact built + ledgered); cost shows in
  Profile → Usage including the agent turn's `assistant.turn` cost.
- **Research→wiki, ingest→wiki, ACH, notes-promote** (prior waves) remain working.
- **Stack:** all 10 compose services up; `/healthz` 200; evals gate `pass_rate 1.0`.

## Architecture rule adherence (the 8 load-bearing rules)

All 8 hold. Highlights:
- **#5 (cost) is now CLOSED** for the agent path — `AgentCostCallbackHandler` on the
  orchestrator + every subagent model writes `ModelCall`+`CostLedgerEvent` (verified
  rows), no double-count with `LiteLLMClient`. (CLAUDE.md updated accordingly.)
- **#2 (gateway-only LLM):** no direct provider SDKs; all `ChatOpenAI` point at the gateway.
- **#3 (agent→service only):** the 6 subagents never touch Postgres/S3 — all via typed
  services / self-called routes.
- **#1 (wiki-first):** the retriever subagent wraps the full `WikiFirstRetrievalRouter`;
  embeddings only in intra-source descent. No secret-RAG shortcut.
- **#4 (ledger per mutation):** incl. the new `POST /reviews/editorial` route.
- **#8 (declarative A2UI):** subagents return render *instructions*; no agent-emitted JS/SQL.
- **No `TODO`/`FIXME`/`NotImplementedError` in production paths** (CI grep gate clean).

## Quality-gate reality (hard numbers, 2026-05-29)

| Gate | Result | Notes |
|---|---|---|
| `pytest -m "not integration"` | 68 pass / **1 fail** | the 1 fail (`aleph-evals/test_runner.py::test_discovers_dataset_dirs_with_manifest`) is **pre-existing**, unrelated to this session |
| evals gate (`--gate strict`) | ✅ pass_rate 1.0 | green |
| `pnpm -C apps/web typecheck` (tsc) | ✅ clean | |
| `pnpm -C apps/web build` | ✅ clean | only chunk-size advisory |
| `alembic check` | ✅ clean | fixed this session |
| `ruff check .` | ❌ **854 errors** | ≈85% cosmetic/auto-fixable: UP037 (197 quoted-annotation), TC001/2/3 (260 type-checking-import-block), PLC0415 (147 in-function imports — a deliberate cycle-break idiom), F401 (85 unused imports), I001 (51 import-sort). 349 auto-`--fix`-able. Few represent real defects. |
| `ruff format --check` | ❌ **125 files** would reformat | never enforced/committed-formatted historically |
| `pyright` (strict) | ❌ **400 errors / 1309 warnings** | dominated by `reportUnknown*` / missing-stub on untyped LangGraph/deepagents/CopilotKit integrations |
| web ESLint (`pnpm lint`) | ❌ **broken** | ESLint 9 finds no `eslint.config.js` (flat-config migration never done) — exits 2 before linting anything |

**Implication (at review time):** CI as written (`.github/workflows/ci.yml` runs ruff check + format + pyright + ESLint) was **red on `main`** and had been for a while — these gates were aspirational, never actually green. That was hygiene debt, not functional breakage. **Now resolved:** the green-the-gates pass cleared the hygiene gates and the CI-pipeline fix corrected the job config (`uv sync --all-packages`, valid `ALEPH_ENV`, MinIO in integration-tests). All five jobs are green on `main` — see the UPDATE banner at the top.

## Prioritized gaps

### P0 — blocks "CI is green" / clean contribution
1. **Ruff is red (854) + unformatted (125 files).** Mostly mechanical. Recommended:
   run `ruff check --fix` + `ruff format` repo-wide (clears the ~726 cosmetic ones),
   then triage the residue (BLE001 blind-excepts, SLF001 private-access, PLW0603
   globals) — keep the deliberate PLC0415 cycle-break idiom via targeted `per-file-ignores`.
2. **Pyright 400 errors.** Largely untyped-framework `reportUnknown*`. Either relax
   `reportUnknown*` for the agent-framework modules (per-file/`pyrightconfig` override)
   or add narrow stubs/casts. Don't chase all 400 under strict — scope it.
3. **Web ESLint broken** — add an `eslint.config.js` (ESLint 9 flat config) so `pnpm
   lint` runs at all. (tsc already passes, so type-safety isn't the gap; lint is.)
4. **Pre-existing unit fail** `aleph-evals test_runner` — fix or quarantine.

### P1 — before an OIDC / production deploy
5. ~~**Runtime agent-facing catalog is stale.**~~ **RESOLVED 2026-05-29** (`d477184`).
   `apps/copilot-runtime/src/server.ts` `ALEPH_A2UI_CATALOG` now advertises all **13
   cards** to the agent (added ApprovalCard, FormCard, MapCard, GraphCard,
   NotebookCellCard, DiffCard; props mirror the shared v0_9 frontend catalog). The 5
   surfaces are intentionally NOT advertised (they're right-panel containers driven by
   the surface stream, not agent-emitted chat cards). Verified in-browser: `build_artifact`
   now reliably renders an ApprovalCard and Approve executes (artifact built + request
   approved).
6. **SSE × OIDC incompatibility** — **DOCUMENTED 2026-05-29** in `docs/security/auth.md`
   (the surface-stream + agent-events SSE + `Bearer local-dev` self-call tools work only
   in `local` mode; EventSource can't send auth headers; self-calls aren't real agent
   tokens — they 401 under `oidc`). The fix design is recorded (query-token/cookie for SSE
   + minted agent tokens for self-calls); **implementation is tracked** (not done — the
   stack is `local`-only, and shipping untested OIDC auth code would be riskier than the
   documented gap). Address before any OIDC/production deploy.
7. **Dependabot vulnerabilities** — **mostly RESOLVED 2026-05-29**. Bumped: lxml→6.1.0
   (high), weasyprint→68.0 (high, + the Dockerfiles now install its glib/pango runtime
   libs — which also makes Builder PDF export work in-container for the first time),
   jinja2→3.1.6, pynacl→1.6.2, pypdf→6.12.2, postcss→8.5.15, prismjs→1.30.0. `ecdsa` (high, no patched version)
   was **RESOLVED 2026-05-29** by migrating `python-jose`→`PyJWT` (cryptography-backed
   RS256/ES256) — `ecdsa`/`python-jose`/`rsa` removed from the lock; verified HS256 +
   RS256/JWKS paths + in-container. The langgraph alert was stale (resolved 1.2.2). **All
   Dependabot high alerts are now cleared.**

### P2 — debt / robustness
8. ~~**New agent surface is largely browser-verified-only.**~~ **ADDRESSED 2026-05-29.**
   Added automated coverage for the agent surface: (a) the SSE **delta pipeline** —
   `split_surface_messages` + `data_model_patches_to_messages` (incl. the array-remove
   "re-set whole array" fallback) now unit-tested in `packages/aleph-a2ui/tests/
   test_surface_streamer.py`, plus a diff→messages round-trip for a real hypotheses delta;
   (b) **subagent delegation wiring** — `apps/api/tests/unit/test_subagents.py` builds all
   six subagents and asserts shape + per-subagent cost tag (`assistant.subagent.<name>`,
   rule #5) + gateway base_url (rule #2); (c) **card builders** in `tests/test_cards.py`;
   (d) an **agent-events integration test** (`tests/e2e/test_agent_events.py`) covering the
   list endpoint's serialization (the same query the SSE `/stream` poll runs) + project
   scoping. Unit suite 69→**97 pass**, integration 12→**14 pass**. The SSE *timer-loop*
   wiring itself (2.5s recompute) is still only browser-verified — its pure components are
   now unit-covered, which is the brittle part.
9. **Docker dev images are baked, lockfile-free for the web image.** `aleph-api`/`aleph-web`
   need `docker compose up -d --build` (no bind-mount/reload); `apps/web/Dockerfile.dev`
   runs `npm install` (no lockfile) — a container-reproducibility nit (CI itself uses the
   committed root `pnpm-lock.yaml`, so CI installs are fine). Document the rebuild gotcha
   in `docs/engineering/local-development.md`.
10. **Known external/op issues** (carried): the Insights LiteLLM gateway's ArgoCD sync is
    wedged on a broken `bootstrap-teams` hook (fix applied live via `kubectl`); AIQ deep
    research can exceed the poll window (shallow is reliable; poller re-enqueues to 30 min).
    `set_model_profile` is read-only (no named-profile-switch route); the editorial reviewer
    is project-scoped (not per-page). All documented in the impl-log / specs.

## Recommended next move

The product is in a strong functional place. The highest-leverage cleanup is a
focused **"green the gates"** pass (P0 #1–#4: `ruff --fix` + `ruff format` + a pyright
scoping config + an ESLint flat config + the eval test) — mostly mechanical, makes CI
trustworthy, and unblocks honest pre-merge checks. After that, **P1 #5 (sync the runtime
agent catalog)** is the one functional gap that visibly limits the agent (it can't
reliably draw half its own cards). Everything else is incremental hardening.
