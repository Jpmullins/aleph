# GOAL — Simplify the platform, own the research loop, rebuild the workspace, purge the drift

**Created:** 2026-07-02 (rev 2 — adds the A2UI-native workspace rearchitecture; supersedes rev 1's
UI-deletion treatment). **Status:** active.
**Origin:** full-codebase audit (5 parallel subsystem reviews) + comparative review of the
`science` app and `llm-wiki` plugin + the decisions to drop AIQ, default storage to local FS,
and go all-in on an A2UI-native workspace.

---

## Progress

Updated by the working agent as packages advance — this table is how a fresh session orients.
Stages: `—` (not started) → `spec` → `spec-approved` → `implementing` → `review` →
`closed (impl-log §<entry>)`.

| WP | Stage | Evidence / notes |
|------|-------|------------------|
| WP-1 | closed (impl-log §WP-1 2026-07-03) | spec: `docs/specs/2026-07-02-wp1-storage.md`; 3-pass adversarial review, 0 violations; integration 61 passed w/o MinIO; in-browser upload→viewer proven |
| WP-2 | — | |
| WP-3 | — | |
| WP-4 | — | |
| WP-5 | — | |
| WP-6 | — | |
| WP-7 | — | |

---

## How this goal executes (the loop)

Every work package below runs the same cycle:

1. **Design spec first.** Write `docs/specs/<date>-<package>.md` before any code. The spec MUST
   contain its own **Final State** section — a falsifiable description of the finished package —
   plus data model, security posture, and what it deletes. A spec that can't say "you can verify
   this by X" for each final-state statement is not done.
2. **Execute** to the spec.
3. **Adversarial review.** A fresh review pass (subagents with no authorship context) checks the
   implementation against the spec's Final State, hunting for: self-fetching shortcuts, dormant
   plumbing, rule violations, untested claims.
4. **Iterate** until the review finds **zero** final-state violations. Only then does the package
   close: full gate suite green, impl-log entry appended with the exact verification commands run.

The goal is complete when **§Final State** below holds in its entirety — that section is the
master success condition. Work packages are the route; the Final State is the destination. If
executing a package reveals the Final State needs amending, amend it *first, explicitly, in a
commit that says so* — never let the destination drift silently to match what got built.

## Rules of engagement (hold at every package boundary)

1. The load-bearing rules in CLAUDE.md hold, with **rule 8 amended** as follows:
   > **8. A2UI surfaces are declarative; sandboxed artifacts are the only escape hatch.**
   > Agents request components by name + props via the catalog. No agent-emitted code executes
   > in the app context. Agent-written code runs only in the sandboxed `code_runner` worker; its
   > outputs are versioned artifacts; interactive artifacts render only inside iframes with
   > `sandbox` isolation (no same-origin, no network). No agent-emitted SQL.
2. **Wire it or delete it.** No component, route, job, or card may exist with zero production
   callers. Deleting is the default; wiring requires a written reason.
3. **No self-fetching surface components.** Right-panel components receive data exclusively via
   A2UI data-model bindings; they never fetch, poll, or subscribe on their own. This is the
   discipline whose absence created the current mess — it is enforced in every review pass.
4. Full gate suite green at every package close:
   ```
   uv run ruff check . && uv run ruff format --check .
   uv run pyright                      # 0 errors; warning count MUST NOT increase
   uv run pytest -m "not integration" -q
   uv run pytest -m integration -q     # against the booted compose stack
   pnpm -C apps/web typecheck && pnpm -C apps/web lint && pnpm -C apps/web build
   cd apps/api && uv run alembic check
   ```
5. Untested = not done. Every "works" claim cites the command or in-browser action that proved it.
6. Never edit an existing Alembic revision. `docs/implementation-log.md` is append-only and
   updated per package; all other docs wait for WP-7.

---

## FINAL STATE (the master success condition)

When this goal is done, all of the following are true and verifiable. Each work package's exit
criteria are the per-slice proofs; this section is the whole.

### F1 — Storage
- `AssetStore` is a protocol with `fs` (default) and `s3` backends selected by
  `ALEPH_ASSET_BACKEND`. A fresh `bootstrap-local.sh` boots with **no MinIO container**.
- All asset bytes (sources, normalized text, rendered artifacts) reach the browser through one
  authenticated API streaming route inside the principal boundary. The words `presigned` and
  `MINIO_PUBLIC_ENDPOINT` do not appear in the codebase.

### F2 — Research
- Research is a native arq worker (`deep_research_job`, LangGraph, gateway-routed models): plan →
  fan out across the project's **allowed** tools → ingest sources via typed routes → reflect with
  bounded loops → report with `[N]` markers → the existing `SynthesisWorkflow` → pending proposal
  in Briefs. Per-project scoping is enforced by tool *binding* (a disallowed connector is never
  bound into the graph).
- `aleph-scholar` provides: tri-state `verify_dois` with retraction detection, `crossref_lookup`,
  `search_openalex`, `expand_citations` (backward + forward), `extract_dois`, the LLM-free
  `style_pass`, and `search_consensus` via the Consensus MCP with OAuth refresh-token auth stored
  in `ConnectorCredential` (bootstrap: `scripts/connect-consensus.py`; rotation on refresh; a
  distinct "reconnect required" state on refresh failure; quota-aware — screening queries only).
- The researcher subagent holds the scholar tools; the reviewer path flags fabricated DOIs and
  retractions in wiki pages (and never flags network-unverifiable ones). Ingested papers carry
  `doi` / `openalex_id` / retraction verdict in `source_metadata_jsonb` with `connector_kind`
  set to the real origin.
- **AIQ does not exist in the repo.** No `aiq` reference in code/compose/scripts, no `nvcr.io`
  image, no `NGC_API_KEY` in `.env.example`; the stack's summed `mem_limit` is ≥4g lower; the
  poll/slot-leak failure class is structurally gone. Every research-run LLM call has a
  `ModelCall` + `CostLedgerEvent`; every ingest has a ledger event.

### F3 — Workspace (A2UI-native)
- **The right panel renders exclusively through the A2UI protocol** against the one shared
  catalog. Zero self-fetching components: no `useQuery`/`fetch`/`refetchInterval`/`EventSource`
  inside catalog component implementations; data arrives only via data-model bindings.
- **Canonical surfaces** (Wiki, Library, Notes, Hypotheses) are built server-side, data-bound,
  and updated by `updateDataModel` **deltas** pushed over the surface SSE stream, woken by
  LISTEN/NOTIFY. No polling intervals anywhere in the right panel. The delta substrate has
  reconnect/resume and ordering tests — it is load-bearing, not dormant.
- **Agent-composed surfaces** exist alongside canonical ones: Briefs is a workbench of
  agent/worker-emitted cards (findings, claims, charts, approvals, research progress, drift
  badges); the agent can pin cards, compose dossiers, and spotlight content conversationally.
- **Wiki reading is first-class.** Markdown remains the wiki's only source of truth (revisions,
  HandEditMarks, claims, curator all operate on it). Rendering is tiered:
  `WikiPageCard` — rich markdown reader with wikilinks as A2UI actions, citation-marker
  popovers, claim-confidence + freshness badges; `HtmlDocCard` — a **deterministic server
  renderer** (not an LLM) compiles page + claims + infobox metadata into styled HTML shown in a
  sandboxed iframe; agent-composed special pages (dossiers, ACH views) are card compositions
  marked *derived, read-only*.
- **The sandbox viz pipeline exists**: a `code_runner` worker executes agent-written Python in an
  isolated container; outputs (PNG/SVG, Vega specs, self-contained HTML) become **versioned
  artifacts** (checksum, producing code, lineage) served via the F1 streaming route; catalog
  components (`ImageCard`, `ChartCard`, `HtmlFrameCard`, …) reference artifacts by URI and render
  interactive ones only in `sandbox` iframes (no same-origin, no network). Rule 8 (amended)
  is enforced by the renderer.
- **The agent has eyes and hands**: CopilotKit shared state exposes the active tab, open wiki
  page, and selection to the agent ("summarize this page" works with no page named); frontend
  actions (`open_page`, `focus_tab`, `pin_to_brief`, `highlight_claim`) let the agent drive the
  workspace mid-conversation, routed through the action router with ledger audit.
- **Every catalog component has both a producer and a renderer** — verified by a committed sweep.
  Components with no producer even in this design are gone.

### F4 — Wiki trust layer
- Pages carry `volatility` (hot/warm/cold) + `verified_at`; a 0–100 freshness score (four 0–25
  dimensions, half-life decay 30/90/365d by volatility) is computed by the curator and visible
  in the workspace (badge + sort).
- A refresh job re-fetches sources for stale pages, fact-diffs against stored text, classifies
  unchanged/updated/contradicted/unreachable, and emits ApprovalCards (skip bumps `verified_at`;
  flag downgrades confidence + renders a banner). It **never auto-recompiles**.
- Sources can be **retracted** (state + ledger event): blast-radius mapping flags every dependent
  claim with a queryable, visibly-rendered `retracted-source` marker; scholar-detected
  retractions feed the same path.
- Briefs/artifacts record their source set + generated-at and show a `drifted` badge when any
  upstream page is newer.

### F5 — Codebase health
- No dormant subsystems: the writer-less assistant persistence layer, superseded/dead routes,
  never-enqueued jobs, orphaned components, and dead fields identified by the 2026-07-02 audit
  are gone (or wired, with written justification). Route-reachability and catalog
  producer/renderer sweeps are committed and pass.
- Confirmed bugs fixed and regression-tested: embedding-dimension guard on **initial ingest**
  (reject before paying for embeddings; re-embed mismatches marked, never re-billed);
  `verify_project_chain` walks `prev_event_id` (not timestamp order); artifact kinds honest
  (implement or 400 — no mislabeled bundles); agent self-calls use real short-lived agent
  tokens (no hardcoded `Bearer local-dev`); Library rename finished; Python pinned to 3.13;
  `.env.example` ↔ `settings.py` reconciled **by a unit test**.
- Pyright warning count strictly below the 2026-07-02 baseline (1,758).

### F6 — Docs
- `docs/` describes only the system that exists. Old specs/assessments/audit live under
  `docs/archive/`; `implementation-log.md` survives append-only; a fresh small doc set
  (architecture, research-loop, workspace, wiki, storage, operations, security — each ≤~200
  lines) is written from the finished code; CLAUDE.md is rewritten to match (no AIQ, no
  MinIO-by-default, amended rule 8, the sweeps as living invariants).
- A fresh review agent given only the new docs finds **zero** claims contradicted by code.
  Every command in CLAUDE.md executes on a fresh clone. `grep -rni "aiq\|minio" docs CLAUDE.md
  README.md` hits only `docs/archive/` and historical impl-log entries.

### F7 — End-to-end proof (the closing demo, performed in-browser and logged)
Project create → URL + PDF ingest → Library viewers render via the streaming route →
"research this topic" → scholar tools + native loop visibly run in Activity → proposal in
Briefs → approve → curator-linked wiki page with freshness badge → "summarize this page" with
the page merely open (shared state) → agent pins a sandbox-generated chart to Briefs (code_runner
→ artifact → iframe) → a lit-review question answered with verified DOIs → retract a cited
source → the wiki page shows the retraction marker and the brief shows `drifted`.

---

## Work packages

Each begins with its design spec (per §How-this-goal-executes) and closes only when its exit
criteria — its slice of the Final State — survive adversarial review.

### WP-1 — Storage: FS default, S3 optional, streaming route
Proves: F1. Spec covers the backend protocol, key layout, the streaming route's auth/ledger
posture, compose changes (MinIO to an opt-in profile), and the deletion list (presign machinery).
Exit criteria: F1 verbatim, plus — upload→Library viewer works in-browser on the `fs` backend;
integration suite green without MinIO; streaming route 401s without auth in oidc mode (unit
test); dead env keys removed.

### WP-2 — `aleph-scholar` + Consensus
Proves: F2 (scholar half). Spec covers the service API, politeness/throttle conventions, the
OAuth bootstrap + credential storage + rotation, quota policy, subagent tool wiring, the
ingest metadata passthrough, and the reviewer citation pass.
Exit criteria: unit — known-good DOI `ok=True` / fake `ok=False` / known-retracted (e.g.
`10.1016/S0140-6736(97)11096-0`) `retracted=True` / mocked network failure `ok=None`;
`expand_citations` non-empty both directions on a well-cited DOI. Live — bootstrap completes
against the real subscription; `search_consensus` returns results using the stored credential
including after access-token expiry; a Live-agent lit question ingests ≥1 paper as a `Source`
with verified DOI + real `connector_kind` + ledger events; reviewer flags a seeded fabricated
DOI and does not flag a network-unverifiable one; no LLM calls inside `aleph-scholar`.

### WP-3 — Native research loop; AIQ deleted
Proves: F2 (loop half + deletion). Spec covers the LangGraph job design (plan/search/ingest/
reflect/compose nodes, loop bounds, plateau cutoff), tool binding + allowlist enforcement,
progress events, failure semantics (worker failure → AgentRun failed, no strands), the
`/synthesize` re-target, and the full AIQ deletion inventory.
Exit criteria: fresh-stack research run produces ≥3 provenanced Sources + draft page + Briefs
proposal, approve → curator runs; every research LLM call cost-attributed (DB assertion);
disabled connector → zero calls (binding unit test + agent-events); `grep -ri aiq` over
code/compose/scripts → nothing; no `nvcr.io`; `NGC_API_KEY` gone; mem_limit sum −4g.

### WP-4 — Workspace rearchitecture (A2UI-native)
Proves: F3. **This is the largest package and gets the most thorough spec** — likely split into
sub-specs: (a) data-binding architecture: canonical surface builders, data-model schemas per tab,
delta emission from LISTEN/NOTIFY, reconnect/resume + ordering semantics, the no-self-fetch
enforcement; (b) reader/editor tier: `WikiPageCard` (wikilink actions, citation popovers, claim
badges), `NoteEditorCard`, the deterministic HTML compiler + `HtmlDocCard` sandboxing; (c) the
sandbox pipeline: `code_runner` worker isolation (image, resource caps, no DB/S3 creds),
versioned-artifact model (checksum, lineage, producing code), iframe sandbox policy, which
catalog components consume artifacts; (d) agent integration: CopilotKit shared-state readables,
frontend actions through the action router, agent composition verbs (pin/compose/spotlight);
(e) the catalog roster: for all 13+ current cards — keep-with-producer, rebuild, or delete,
each with its producer named.
Exit criteria: F3 verbatim, plus — `grep -rn "useQuery\|refetchInterval\|EventSource\|fetch("
apps/web/src/a2ui/components` → nothing; delta reconnect/resume + ordering integration tests;
in-browser: edit a hypothesis in one tab and watch the card patch in place with no refetch;
agent opens a wiki page and pins a sandbox-produced chart mid-conversation; the producer/renderer
sweep passes; markdown remains the only wiki write-format (attempted HTML write path does not
exist).

### WP-5 — Dead-code, bug, and drift purge
Proves: F5. Runs **after** WP-3/WP-4 so the AIQ deletion and the WP-4 catalog roster settle most
UI questions first. Spec is the audited kill/fix list finalized against what WP-1..4 already
removed; datasets default: delete broken UI paths + unproduced routes, keep ORM tables.
Exit criteria: F5 verbatim (sweeps committed and green; each bug fix has a named regression
test; warning count recorded before/after in the impl-log).

### WP-6 — Wiki trust layer
Proves: F4. Spec covers the schema migration, the freshness math, refresh-job mechanics +
ApprovalCard actions, retraction state machine + blast-radius query, drift computation, and how
badges/banners render as WP-4 catalog components.
Exit criteria: F4 verbatim, exercised by integration tests (fixture-aged sources → refresh
ApprovalCard; skip vs flag behaviors; retraction propagates to two dependent pages + ledger;
newer upstream revision → `drifted` badge) and verified in-browser.

### WP-7 — Docs reset
Proves: F6. Runs last. Spec is the doc inventory: what archives, what gets written, the
CLAUDE.md rewrite outline, and the drift-prevention tests.
Exit criteria: F6 verbatim, including the fresh-agent zero-contradictions review (report
attached to the impl-log) and the scripted CLAUDE.md-commands check.

---

## Explicitly out of scope (this goal)
- The full figure-composition stack (`figure-composer`/`paper-narrative` multi-agent loops) —
  next goal; WP-4's versioned artifacts + sandbox are its foundation.
- Full OIDC deployment hardening beyond WP-5's agent-token fix (SSE token transport for
  EventSource remains a documented gap in `docs/security.md`).
- Multi-project knowledge, real-time co-editing, and everything excluded by the original spec
  §16.1.

## Definition of done
Every Final State statement (F1–F6) verified, every work package closed through its
spec→execute→review→iterate cycle with zero remaining violations, the full gate suite green,
and the F7 end-to-end demo performed in-browser and logged step-by-step in the impl-log.

---

## Running this with `/goal`

`/goal` re-prompts the working agent after every turn until a small evaluator model — which
reads **only the conversation transcript**, never files — confirms the condition. That imposes
the protocol below on the working agent, and the condition format below on the user.

### Session protocol (binding on the working agent whenever a /goal targets this file)

1. **Start of every turn:** read `GOAL.md`; consult §Progress for the active package and its
   stage; resume exactly there in the cycle (spec → approval → implement → adversarial review →
   iterate). Never restart completed work; never skip the spec stage.
2. **End of every turn:** print a **goal status block** — the active WP + stage, then the WP's
   exit criteria as a checklist, each marked `MET` (with the command output or in-browser action
   that proved it, shown in this conversation) or `UNMET` (with the concrete next step).
   Evidence not displayed in the transcript does not exist to the evaluator.
3. A criterion may be marked `MET` only if its proof was displayed in the current session, or
   it was recorded in a prior session's impl-log entry **and** its cheap checks (greps, unit
   tests) were re-run and re-shown now.
4. **On package close:** update §Progress, append the impl-log entry, and display the final
   checklist together with the green full-gate-suite output.
5. §Final State is never amended to match what got built. Amendments are explicit, argued
   commits made before any implementation relies on them.
6. Interactive checkpoints live **outside** goals where possible: get spec approval before
   setting an implementation goal; WP-2's Consensus OAuth login needs the user at a browser —
   pause and ask when reached, and repeat the ask in the status block until answered.

Run goals with auto mode on (tool-call approvals) — `/goal` only removes per-turn prompts.
Turn-bound clauses reset when a session is resumed; treat them as per-sitting limits.

### Ready-made conditions (copy-paste; one per session, `/clear` between packages)

The generic template — substitute the package id:

```
/goal WP-<N> of GOAL.md is closed: a design spec with its own Final State section exists in
docs/specs/; every WP-<N> exit criterion has been displayed in this conversation marked MET
with its verifying evidence; an adversarial review by fresh subagents was shown reporting zero
Final State violations; the full gate suite output was shown green; GOAL.md §Progress marks
WP-<N> closed and docs/implementation-log.md contains the close entry. Stop after 40 turns
this sitting if unmet.
```

Package-specific additions to append to the template:

- **WP-1:** "Evidence must include the in-browser upload→Library-viewer check on the fs
  backend with no MinIO container running, and the empty grep for presigned/MINIO_PUBLIC_ENDPOINT."
- **WP-2:** "Evidence must include the four verify_dois unit outcomes (good/fake/retracted/
  network-None), a live search_consensus result via the stored credential, and the Live-agent
  lit-question flow ingesting a Source with verified DOI. If the Consensus OAuth login is
  pending, the turn must end by asking the user to complete it."
- **WP-3:** "Evidence must include the fresh-stack research round-trip (≥3 provenanced Sources,
  Briefs proposal, approve → curator), the cost-attribution DB assertion, and the empty
  `grep -ri aiq` over code/compose/scripts."
- **WP-4 (two goals):** first `/goal docs/specs/ contains the WP-4 design spec (sub-specs a–e
  per GOAL.md) and the user has replied in this conversation explicitly approving it; stop
  after 10 turns if unmet` — then, in a new session, the template with: "Evidence must include
  the empty self-fetch grep over apps/web/src/a2ui/components, the delta reconnect/resume test
  output, and the in-browser demo: hypothesis edit patching in place, and the agent opening a
  wiki page + pinning a sandbox-produced chart mid-conversation."
- **WP-5:** "Evidence must include the route-reachability and producer/renderer sweep outputs,
  each named regression test passing, and the pyright warning counts before/after."
- **WP-6:** "Evidence must include the integration-test outputs for refresh (skip vs flag),
  retraction blast-radius across two pages, and the drifted-badge check, plus in-browser
  freshness badges."
- **WP-7:** "Evidence must include the fresh-review-agent zero-contradictions report, the
  scripted CLAUDE.md-commands check output, and the archive-only grep for aiq/minio."

Whole-goal variant (maximum autonomy, less control):

```
/goal All work packages WP-1..WP-7 in GOAL.md are closed per the spec→execute→review→iterate
cycle and Final State F1–F6 hold: the completed F1–F6 checklist was displayed with verifying
evidence per item, the F7 end-to-end demo was performed in-browser and its steps logged in the
conversation and impl-log, and GOAL.md §Progress shows all seven packages closed. No package
counts as closed without a displayed adversarial review reporting zero violations.
```
