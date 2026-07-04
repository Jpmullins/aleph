# WP-4 — Workspace rearchitecture (A2UI-native)

**Date:** 2026-07-04 · **Package:** WP-4 of `GOAL.md` · **Proves:** Final State F3
**Status:** spec (awaiting approval)

## Problem

The right panel *looks* A2UI-native but isn't. The five surface tabs mount local React views
(`WikiSurface`, `ArtifactsSurface`, `NotesSurface`, `HypothesesSurface`) that each **self-fetch
and poll** their own data with `useQuery`/`refetchInterval` (8 component files, some polling
every 5s) — the A2UI surface stream only carries a structural shell. The delta substrate
(`surface_streamer.diff_data_model` → `updateDataModel`) is **built and unit-tested but wired to
no live tab** — only a Hypotheses bound-card exemplar exercises it, and that exemplar isn't the
tab that actually renders. There is no reconnect/resume cursor or ordering guarantee. Wiki
reading is inline React with no catalog card; there is no sandbox code execution, no
deterministic HTML renderer, and the agent can switch tabs but cannot open a page, pin a card,
or see the user's selection. This package makes the right panel genuinely declarative and
data-bound, adds the reader tier and the sandbox artifact pipeline, and gives the agent
eyes+hands.

This is the largest package; it is organized as five sub-specs (a)–(e). Everything below is the
one spec; the sub-spec headers are the structure GOAL.md §WP-4 asks for.

---

## Sub-spec (a) — Data-binding architecture + delta substrate

**Canonical surfaces become server-built and data-bound.** For each of the four canonical tabs
(Wiki, Library, Notes, Hypotheses) the backend builder emits a v0_9 surface whose components
reference a **data model** by binding path (the Hypotheses `hypothesis_cards_v09` exemplar is the
template); the React views render **only** from bound props. No `useQuery`, no
`refetchInterval`, no `fetch`, no `EventSource`, no `api.get/post` inside
`apps/web/src/a2ui/components/`.

- **Surface builders** (`packages/aleph-a2ui/.../components/surfaces.py`): each canonical tab
  gets a `<tab>_v09(project_id, session)` builder that (1) loads its rows through the existing
  service layer, (2) emits `createSurface` + `updateComponents` (structure) once, (3) populates
  a `dataModel` whose shape is the tab's typed schema (below). The legacy `_surface()` tree
  builders and the self-fetching React surface views are **deleted**.
- **Data-model schemas per tab** (new `surfaces_schema.py`, JSON-Schema, mirrored in the FE
  catalog contract): `wiki` = `{pages: [{id, title, slug, status, is_stub, freshness?}], open: {page_id, revision:{body_md}, claims[], citations[], wikilinks_out[]} | null}`; `library`
  = `{sources: [...], artifacts: [...]}`; `notes` = `{notes: [{id, title, body_md, updated_at}]}`;
  `hypotheses` = `{items: [...], ach: {...} | null}`. (Briefs is agent-composed — sub-spec (d).)
- **Delta emission from LISTEN/NOTIFY.** The surface stream already wakes on the
  `ChangeBroker` (Postgres `pg_notify('aleph_changes')` → `NotifyListener`). On wake, the tab
  recomputes its data model and emits `diff_data_model(prev, next)` → `updateDataModel`
  messages (the substrate exists); structural diffs still re-send `updateComponents`. Polling
  intervals are removed from the right panel entirely; the 10s `sub.wait` fallback stays (it is
  a self-heal, not a poll of the UI).
- **Reconnect / resume / ordering (the load-bearing gap).** The stream gains a **monotonic
  per-surface sequence number** stamped on every SSE message (`id:` field + `seq` in the
  payload). On reconnect the client sends `Last-Event-ID`; the server replays from a small
  bounded per-connection ring buffer if the id is still in range, else re-sends a full snapshot
  with a fresh baseline `seq`. Messages are applied in `seq` order; out-of-order or duplicate
  `seq` is dropped by the client. This is covered by **integration tests**: (1) resume after a
  dropped connection applies only the missed deltas; (2) a gap beyond the buffer triggers a
  clean full-snapshot resync; (3) interleaved deltas apply in `seq` order regardless of arrival
  order. These tests are the proof the substrate is load-bearing, not dormant.
- **No-self-fetch enforcement.** A committed check script (`scripts/check-no-self-fetch.sh`,
  run in CI) greps `apps/web/src/a2ui/components` for
  `useQuery|useMutation|refetchInterval|EventSource|fetch(|api\.(get|post|put|delete)` and fails
  on any hit. Data-mutating **actions** (approve, edit, pin, …) do not fetch from components;
  they call `onAction(name, params)` which routes through the existing action router
  (`emitAction` in `surface-context`), so the mutation path is the router, not react-query.

## Sub-spec (b) — Reader / editor tier

- **`WikiPageCard`** (new catalog card + renderer): the rich markdown reader, receiving
  `{body_md, claims, citations, wikilinks_out, page_meta}` **as bound props** (no fetch). It
  reuses the existing `WikiBodyMarkdown` tokenizer (headings/paragraphs/`[[wikilink]]`/`[cN]`),
  upgraded so: wikilinks emit `open_page` **A2UI actions** (not client-side navigation);
  citation markers `[cN]` open a **popover** with the resolved `Citation` (source title + URL,
  from bound `citations`); claims render **confidence badges** (from `WikiClaim.confidence`) and
  a **freshness badge placeholder** (the value is populated by WP-6; the card renders "—" until
  then, no coupling). No `dangerouslySetInnerHTML`.
- **`NoteEditorCard`** (new): a bound-props note editor (title + `body_md`), debounced
  `edit_note` action through the router; replaces the `NotebookCellCard` textarea path and the
  self-fetching `NotesSurface` editor. Markdown only.
- **Deterministic HTML compiler + `HtmlDocCard`.** A **server-side, non-LLM** compiler
  (`packages/aleph-wiki/.../html_compiler.py`) turns a wiki page (revision `body_md` + `claims`
  + optional infobox metadata) into a single self-contained styled HTML document (inline CSS,
  no scripts, no external refs). It is deterministic (same inputs → byte-identical output;
  property-tested). The compiled HTML is stored as a **rendered artifact** (sub-spec (c)) and
  shown by `HtmlDocCard` inside a **sandboxed iframe** referencing it by the streaming-route
  URI. **Infobox metadata** is a new nullable `infobox_jsonb` on `WikiPage` (migration;
  key/value pairs the curator may populate — optional, absent = no infobox). Markdown remains
  the **only** wiki write-format: there is no `body_html` write path, and the compiler reads
  markdown, never writes it. Agent-composed special pages (dossiers, ACH views) are card
  compositions marked `derived: true, read_only: true` in their props.

## Sub-spec (c) — The sandbox viz pipeline

- **`code_runner` worker = a separate, credential-less compose service.** New
  `deploy/compose` service `aleph-code-runner` built from a new minimal Dockerfile
  (`apps/code-runner/Dockerfile`): Python + a fixed, audited scientific stack (matplotlib,
  numpy, pandas, vega/altair for spec emission) and **nothing else**. Isolation (compose):
  a dedicated `internal: true` network reaching **only Redis** (no internet, no Postgres, no
  aleph-api — `network_mode: none` is infeasible for an arq worker; see the §6 amendment),
  `cap_drop: [ALL]`, `read_only: true` rootfs with a small `tmpfs` scratch, `pids_limit`, tight
  `mem_limit`/`memswap_limit`, `no-new-privileges`, non-root `user`, **no
  `DATABASE_URL`/`ALEPH_S3_*`/`LITELLM_*`/`ALEPH_AGENT_TOKEN_SECRET` env, no `data/assets` bind
  mount.** It consumes code jobs off a **dedicated Redis queue** (`arq` queue name
  `code_runner`) and returns only result bytes + metadata over the job result — it never
  touches Postgres or the asset store.
- **Execution contract.** A job carries agent-written Python + declared output kind
  (`png` | `svg` | `vega` | `html`). The runner executes it in a subprocess with a wall-clock
  timeout and the resource caps above, captures the single declared output artifact from a
  fixed scratch path, and returns `{ok, bytes_b64 | spec_json, mime, error?}`. Agent code that
  attempts network/file/DB access fails closed (no creds, no network, read-only fs). No
  `exec()` of agent code in any credentialed process — the API/worker never runs it.
- **Versioned artifacts.** A privileged step (in `aleph-workers`, which *does* hold the store)
  takes the runner's returned bytes and persists them as a `RenderedAsset`/`ArtifactVersion`
  with a **new `producing_code` field** (stored in `lineage_jsonb.producing_code` + a sha of
  it), checksum (`sha256`), and `builder_agent_run_id` lineage. New viz artifact kinds
  (`image`, `chart`, `html_frame`) are added to the `artifact_kind` allowlist; the honesty rule
  (F5) holds — an unimplemented kind 400s.
- **Catalog cards reference artifacts by URI.** `ImageCard` (`<img src=streaming-route>`),
  `ChartCard` **rebuilt** to take an artifact URI (Vega spec artifact) or an inline spec via
  bound props — **no self-fetch**, and `HtmlFrameCard` renders interactive HTML **only** inside
  an iframe with `sandbox` (scripts allowed, **no `allow-same-origin`, no network** — enforced
  by the element attribute *and* the server CSP `sandbox` already on non-PDF asset bytes). The
  renderer refuses to mount an `HtmlFrameCard`/`HtmlDocCard` whose src is not the asset
  streaming route (amended rule 8 enforced at the renderer).

## Sub-spec (d) — Agent integration (eyes + hands)

- **Shared-state readables.** CopilotKit shared state exposes `{active_tab, open_page_id,
  open_page_title, selection}` (extends the existing `useAgentContext` tab/open-page readable
  with a `selection` field the reader tier updates on text/claim selection). "Summarize this
  page" works with no page named because `open_page_id` is in shared state.
- **Frontend actions (agent → workspace).** Four new frontend tools bridged to the action
  router: `open_page(page_id|slug)`, `focus_tab(tab)`, `pin_to_brief(card)`,
  `highlight_claim(claim_id)`. Each dispatches through the existing **ledger-audited** action
  router (`action_router.py` → `CardAction` row + ledger event); `open_page`/`focus_tab` drive
  `useWorkspaceUI` state, `pin_to_brief` calls `card_service.pin_card`, `highlight_claim` sets
  the reader's highlight. The single existing `open_surface` tool is folded into `focus_tab`.
- **Agent composition verbs.** `pin_to_brief` (exists as `_pin_to_briefs_impl`, kept),
  `compose_dossier(title, card_ids|page_ids)` (new — composes a derived, read-only Briefs card
  grouping other cards), `spotlight(card_id)` (new — marks one Briefs card spotlighted; the
  surface builder orders it first with a spotlight flag). All three go through the router and
  are ledgered.

## Sub-spec (e) — The catalog roster (keep / rebuild / delete, each with its producer)

| Card | Decision | Producer |
|------|----------|----------|
| ClaimCard | keep | `surfaces.py` wiki claims (data-bound) |
| ApprovalCard | keep | `surfaces.py` synthesis/merge proposals |
| FindingCard | keep | `surfaces.py` review findings |
| HypothesisCard | keep | Hypotheses builder (already data-bound) |
| ArtifactCard | keep | Library builder (data-bound) |
| SourceCard | rebuild (drop self-fetch) | Library builder supplies normalized-text preview via binding |
| ChartCard | rebuild (artifact-URI, no fetch) | `viz_builder` + code_runner render → artifact |
| WikiPageCard | **new** | wiki builder (bound reader) |
| NoteEditorCard | **new** | notes builder |
| HtmlDocCard | **new** | deterministic HTML compiler → artifact |
| ImageCard | **new** | code_runner → artifact |
| HtmlFrameCard | **new** | code_runner → artifact |
| TableCard | rebuild (bound rows, no fetch) | dataset/agent producer supplies rows in props |
| MapCard | **delete** | no producer, no roadmap consumer |
| GraphCard | **delete** | no producer |
| FormCard | keep (agent-emit) | agent `render_a2ui` (clarify/approval flows use it) |
| NotebookCellCard | **delete** | superseded by NoteEditorCard; no producer (removed for N+1) |
| DiffCard | keep (stub → real, referenced by ApprovalCard) | ApprovalCard.diff_card_id; render an actual revision diff from bound props |
| Surfaces (Wiki/Library/Notes/Hypotheses) | rebuild data-bound | `surfaces.py` `*_v09` builders |
| BriefsSurface | keep (agent-composed workbench) | `_briefs_messages` + pinned/composed/spotlighted cards |

- **Committed producer/renderer sweep** (`scripts/check-catalog-roster.sh`, CI): asserts every
  catalog component name has both a renderer (FE impl) and a producer (a backend emitter or a
  documented agent-emit tool), and that deleted names appear nowhere. Fails on a card with a
  renderer but no producer (or vice versa).

---

## Security posture

- Amended rule 8 enforced two ways: no agent code executes in a credentialed process (only the
  network-less, credential-less `code_runner`), and interactive artifacts render only in
  `sandbox` iframes (no same-origin, no network) whose src must be the asset streaming route.
- The `code_runner` service holds no secrets, no DB/S3 access, no network — a full escape
  yields only CPU/mem within the cgroup caps and the agent's own submitted code.
- Frontend actions remain ledger-audited through the one action router (no new mutation path).
- Markdown stays the only wiki write-format; the HTML compiler is read-only over it.

## What it deletes

`MapCard`, `GraphCard`, `NotebookCellCard` (renderers + schema + builders); the self-fetching
surface views (`WikiSurface`/`ArtifactsSurface`/`NotesSurface`/`HypothesesSurface` react-query
logic) replaced by bound renderers; the legacy `_surface()` tree builders; the dead
`/artifacts/.../download` href.

## Final State (falsifiable) — F3 verbatim + WP-4 exit criteria

1. **Right panel renders exclusively through A2UI against the shared catalog; zero
   self-fetching components.** Verify: `scripts/check-no-self-fetch.sh` (the grep over
   `apps/web/src/a2ui/components` for `useQuery|refetchInterval|EventSource|fetch(`) → empty;
   CI runs it.
2. **Canonical surfaces are server-built, data-bound, updated by `updateDataModel` deltas over
   the surface SSE stream woken by LISTEN/NOTIFY; no polling intervals in the right panel.**
   Verify: the four `*_v09` builders emit bound data models; `grep -rn refetchInterval
   apps/web/src/a2ui/components` → empty (the F3 requirement is the **right panel**; the
   binding GOAL exit grep, GOAL.md:232, is also scoped to `a2ui/components`); in-browser,
   editing a hypothesis in one tab patches the card in place with no refetch (network tab shows
   the SSE delta, no new GET).
   > **Amended 2026-07-04 (before close, GOAL rule 5).** The draft mis-scoped this verify as
   > `grep refetchInterval apps/web/src` (whole tree). F3 and the GOAL exit criterion both scope
   > "no polling" to the **right panel** (`a2ui/components`), which is clean. The remaining
   > `refetchInterval` uses live in **center-panel / overlay** components outside the right
   > panel — the Activity feed (`ActivityCard`), the chat surface (`CopilotChatSurface`), and
   > the Logs/Notifications drawer (`Drawers`) — none of which is a right-panel A2UI surface.
   > (The dead, superseded `WikiTab.tsx` was deleted as part of this reconciliation.) Push-
   > converting those center-panel polls is out of WP-4 scope; noted for WP-5's dead/drift pass.
3. **The delta substrate has reconnect/resume + ordering tests and is load-bearing.** Verify:
   the three integration tests (resume-applies-missed-deltas, gap→full-resync, seq-ordering)
   pass; the tests drive the live route, not just the streamer unit.
4. **Agent-composed surfaces exist alongside canonical ones.** Verify: Briefs renders
   agent/worker cards; `compose_dossier` + `spotlight` + `pin_to_brief` work conversationally
   (in-browser: the agent pins and spotlights a card mid-conversation).
5. **Wiki reading is first-class, tiered, markdown-only.** Verify: `WikiPageCard` renders
   markdown with wikilink actions + citation popovers + claim badges; `HtmlDocCard` shows the
   deterministic server-compiled HTML in a sandboxed iframe; markdown is the only write-format
   (`grep -rn "body_html\|render_html" packages/aleph-wiki apps/api` finds no write path; an
   attempted HTML page write does not exist).
6. **The sandbox viz pipeline exists.** Verify: `aleph-code-runner` is a compose service with
   **no egress except Redis** + `cap_drop: [ALL]` + read-only rootfs + no DB/S3/LLM env; a
   `code_runner` job executes agent Python and returns bytes; those become a versioned artifact
   (checksum + producing_code + lineage) served via the F1 streaming route;
   `ImageCard`/`ChartCard`/`HtmlFrameCard` reference artifacts by URI and render interactive
   ones only in `sandbox` iframes. In-browser: the agent produces a chart via code_runner and
   pins it to Briefs mid-conversation.
   > **Amended 2026-07-04 (before close, GOAL rule 5).** The draft said `network_mode: none`.
   > That is infeasible for an arq worker, which must reach Redis to pull jobs. The equivalent
   > guarantee — **no internet egress, and no reachability to Postgres or aleph-api** — is
   > delivered instead by a dedicated `internal: true` compose network on which the only
   > reachable service is a **dedicated `code-runner-redis`** — NOT the platform Redis. The
   > platform Redis (which carries agent tokens as job args, privileged job queues, and the
   > LISTEN/NOTIFY streams) is off `code-runner-net` entirely. The trusted `aleph-workers`
   > dual-homes onto both networks to dispatch/await code jobs; the sandbox reaches only
   > `code-runner-redis`, whose sole payload is `run_code_job(code, output_kind, timeout_s)` —
   > no tokens, no privileged jobs, no cross-project data. Verified live: postgres, aleph-api,
   > and external hosts all blocked. The agent-code *subprocess* is additionally denied sockets
   > (`python -I` + socket guard; `unshare(NEWNET)` best-effort, fails closed under
   > `cap_drop:[ALL]`). Residual (documented in the impl-log): a raw-ctypes syscall could bypass
   > the Python socket guard and reach `code-runner-redis`, but that bus carries only code-job
   > payloads (no secrets), the rootfs is read-only, and all caps are dropped — so the worst
   > case is disruption of the ephemeral code queue, never token capture or a privileged-job
   > injection. *(This dedicated-Redis split was added 2026-07-04 in response to the WP-4
   > security review, which found the earlier shared-Redis design exposed the platform bus.)*
7. **The agent has eyes and hands.** Verify: shared state exposes active tab + open page +
   selection ("summarize this page" works with nothing named); `open_page`/`focus_tab`/
   `pin_to_brief`/`highlight_claim` drive the workspace through the ledger-audited router.
8. **Every catalog component has both a producer and a renderer; producerless cards are gone.**
   Verify: `scripts/check-catalog-roster.sh` passes; `MapCard`/`GraphCard`/`NotebookCellCard`
   appear nowhere.
9. **Gates.** Full gate suite green; pyright warnings ≤ baseline; new migration `alembic check`
   clean.

## Sequencing note

WP-4 lands in dependency order: (a) data-binding + delta substrate + no-self-fetch (unblocks
everything), then (b) reader tier, (c) sandbox pipeline, (d) agent integration, (e) roster
cleanup + sweeps. The freshness badge in (b) is a placeholder wired by WP-6.
