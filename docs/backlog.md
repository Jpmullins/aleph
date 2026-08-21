# Backlog — what we discussed and what is actually built

Written 2026-08-21. Every entry states what was asked for, what exists in the
tree today, and what is missing. Verified against the code, not remembered.

Status key: **NOT BUILT** · **PARTIAL** · **BROKEN** · **DONE**

---

## A. The core product thesis — plugins

This is the largest gap. CLAUDE.md opens by saying *"an agent that authors
plugins for itself and activates or deactivates them as needed… The kernel is
the product."* Almost none of that exists.

### A1. Plugin system — **NOT BUILT**

**Asked for:** "everything is a plugin", taken from deepseek-harness/cordis.
Plugins compile and communicate efficiently enough to feel like one system.

**Exists:** `packages/aleph-kernel` has effects, capabilities, a boot manifest,
an AST gate, skills and a spawn ledger. Ten capabilities mount at boot.

**Missing:** there is no `Plugin` type, no registry, no `activate` /
`deactivate`, no lifecycle, no dependency resolution, no isolation boundary. A
"plugin" today is a `uv` workspace package wired in by hand at the composition
root. Nothing can be added or removed at runtime.

### A2. Agent authors its own plugins — **NOT BUILT**

**Asked for:** the agent writes plugins for itself, with guardrails preventing
it from removing load-bearing capability.

**Missing:** entirely. No authoring path, no guardrail, no blast-radius check
against removal. The kernel computes blast radius for effects, which is the
primitive this would be built on.

### A3. Per-plugin A2UI catalogs — **NOT BUILT**

**Asked for:** "every plugin can come with its own catalog for A2UI and publish
it."

**Exists:** exactly one catalog —
`packages/aleph-a2ui/src/aleph_a2ui/catalog.json` — and a sweep
(`check-single-catalog.sh`) that *enforces* there is only one.

**Missing:** catalog composition. To support per-plugin catalogs the sweep's
invariant has to change from "one catalog" to "one catalog per plugin, merged
without id collisions", and the generator has to merge rather than copy.

### A4. Per-plugin settings cards — **NOT BUILT**

**Asked for:** "every plugin can have a fully scoped settings card"; three tiers
by trust level.

**Exists:** one hand-written `SettingsBody` in `apps/web/src/components/Drawers.tsx`
covering model profile and connectors.

**Missing:** a settings contract a plugin declares, a renderer that composes
declared cards, and the trust tiering.

---

## B. Settings and configuration

### B1. Settings is a drawer, not part of the workbench — **NOT BUILT**

**Asked for:** rethink the UI around the board/pane model; settings should not
be a bolted-on side panel.

**Exists:** `Drawers.tsx` renders settings in a slide-over. The pane registry
serves seven panes and **settings is not one of them** — verified.

**Missing:** settings as a pane (or panes) on the board, so it obeys the same
model as everything else and per-plugin cards have somewhere to land.

### B2. Cannot set the model endpoint — **NOT BUILT**

**Asked for, repeatedly:** "aleph will not serve any models. We will connect to
openai compatible endpoints (litellm, ollama, vllm, bedrock, etc.)" — and the
endpoint must be settable, not baked in.

**Exists:** `litellm_base_url` is read once from env in
`apps/api/src/aleph_api/settings.py`. Discovery (`aleph_models.discovery`) reads
whatever that gateway serves, and the Settings picker lists those models.

**Missing:** any way to change the endpoint without editing `.env` and
restarting. There is no route, no UI field, no per-project override, no
credential store for it, and no "test this endpoint" probe before saving.

This is the one that blocks using Aleph against anything but the currently
configured gateway.

### B3. Model profile PATCH returned 500 — **FIXED (this session)**

`updated_at` has a server-side `onupdate`, so the flush left it expired; reading
it lazy-loaded and raised `MissingGreenlet`. Two of the three write paths
already had `await session.refresh(p)`; the PATCH handler did not.

### B4. Every server error surfaced as a CORS failure — **FIXED (this session)**

`CORSMiddleware` was added first, which `add_middleware` makes *innermost*, so
error responses produced by `ErrorMiddleware` never passed back through it. Any
500 reached the browser with no `Access-Control-Allow-Origin`, and the console
blamed CORS instead of showing the real error. CORS is outermost now.

---

## C. Generative UI spectrum

CopilotKit names four bands. Aleph now uses three.

| Band | Status |
|---|---|
| Controlled (`useComponent`) | **DONE** |
| Declarative (A2UI catalog) | **DONE** |
| MCP Apps | **NOT BUILT** — discussed, never started |
| Open-ended (`generateSandboxedUi`) | **DONE** — with a design skill and two read-only sandbox functions |

### C1. CopilotKit Enterprise Intelligence — **EVALUATED, mostly already built**

Asked about three times and never actually looked at. Researched via the
CopilotKit docs MCP. Here is the real answer.

**What it is:** a separate backend service beside the runtime, licensed and
commercial. It provides:

| Capability | Aleph's equivalent today |
|---|---|
| Durable, resumable threads (Postgres + Redis, single-writer locks, WebSocket sync) | LangGraph checkpoints in Postgres — **exists**, but resume is unreliable (see below) |
| Event timelines / thread history | The action ledger — hash-chained, append-only — **exists and is stronger** |
| Agent trace inspection | OTEL + Langfuse, behind `--profile tracing` — **exists** |
| Multi-tenancy (org / project / user) | `project_id` + `access_scope` on every row — **exists** |
| Inspector & Admin Console (hosted web UI) | **Nothing.** This is the real gap. |
| API key management, project selection | Not applicable — Aleph is single-tenant per deployment |

**The deployment problem.** Two options, and both conflict with how Aleph is
built. Cloud-hosted sends every thread to CopilotKit's infrastructure, which is
the opposite of a stack that runs its own gateway specifically so nothing leaves.
Self-hosted installs the `copilot-intelligence` Helm chart **into Kubernetes**,
with CopilotKit engineering involved in the deployment. Aleph deploys with
`docker compose`, which was an explicit requirement.

**Recommendation: do not adopt it.** Aleph has already built the self-hosted
equivalent of four of its five capabilities, and its ledger is a stronger
version of the event history. Taking it on would mean either giving up data
residency or adopting Kubernetes, to gain things that already exist.

**But take the one idea that is missing — the Inspector.** A surface that shows
the AG-UI event stream, the tool calls, and where a run failed. Aleph has none,
which is why the `RUN_ERROR` in E1 is still untraced and why diagnosing the chat
path means reading container logs. This is buildable directly on Aleph's own
data: the ledger already records every action, and the agent-events SSE stream
already exists. Filed as **C3**.

### C2. MCP Apps — **NOT BUILT**

The third band. Would let an MCP server ship its own UI, which is close to what
A3 wants for plugins.
### C3. An agent Inspector pane — **NOT BUILT**

The one genuinely valuable idea from Enterprise Intelligence, rebuilt on data
Aleph already has: AG-UI event stream, tool calls with arguments and results,
run status, and the failure point when a run errors. A pane, not a hosted
console. Would have shortened every agent debugging session in this project.

---

## D. Known broken, carried in CLAUDE.md

These predate this session and are still true.

- **D1. `commit_revision` is not atomic on the path agents use.** It row-locks
  only when given a `page_id`; the by-title path computes `revision_no` as
  `max+1` on an unlocked page. Concurrent commits to the same title can collide.
- **D2. Cost attribution has a hole.** `AgentCostCallbackHandler` writes a
  `ModelCall` only when the response carries usage *and* a project id resolves.
  A `ChatOpenAI` response without usage is silently uncosted.
- **D3. The runtime bridge does not forward the caller's credential.**
  `apps/copilot-runtime/src/server.ts` builds `new HttpAgent({ url })` with no
  headers, so `oidc` mode cannot authenticate the chat path. `local` mode — the
  only deployed mode — is unaffected.
- **D4. SSE cannot carry a bearer token.** `EventSource` sets no
  `Authorization` header, so in `oidc` mode every SSE stream and the
  `<iframe>`-consumed asset route have no token transport. Deliberately out of
  scope until OIDC deployment is taken up as a whole.

---

## E. Defects observed in the running stack (2026-08-21)

Taken from a live browser console and the API logs. Not yet diagnosed unless
noted.

- **E1. Agent run fails, then the runtime tries to keep streaming.**
  `AGUIError: Cannot send event type 'TOOL_CALL_START': The run has already
  errored with 'RUN_ERROR'`. The secondary error is a symptom; the primary
  `RUN_ERROR` has not been traced yet. Reproduces on the newest project.
- **E2. `POST /scholar/search` returns 503** repeatedly in the API log.
- **E3. Compiled HTML assets are broken in the reader.** The sandboxed iframe
  blocks scripts (`allow-scripts` not set) and the page preloads
  `/_fs-ch-*/assets/inter-var.woff2`, which 404s and is also blocked by CORS
  from the `null` origin. So server-compiled page HTML renders unstyled.
- **E4. Agent model calls cannot be priced.** `claude-sonnet-4-6` is recorded
  with `pricing_source=unknown`. Correct behaviour (never a silent `$0`) but it
  means agent spend is unpriced in practice — the gateway is not reporting rates
  for that model and no hint file fills them.
- **E5. Gateway rate limiting.** Reported as "weirdly rate limited"; not yet
  characterised. Needs a look at what Aleph sends per turn — the subagent fan-out
  is the likely cause.
- **E6. Lit dev-mode warning** from a CopilotKit dependency. Cosmetic.

---

## F. Done, for an honest baseline

So the list above is read as "what remains", not "what exists".

- Kernel boot for API and workers, ten capabilities with live read-path probes
- Corpus-wide hybrid retrieval (recall@1 0.91 hybrid vs 0.60 lexical, measured)
- Claim Spine write path, claim → chunk → char-span grounding
- Gateway-driven model discovery, autoconfigure by requirements, pricing
  provenance (`gateway` / `static` / `unknown`)
- The board/pane workspace, server-driven pane registry, one multiplexed SSE
  connection for the reading region
- Open Generative UI with an Aleph design skill and read-only sandbox functions
- The wiki restructure: schema governance, frontmatter, 16 lint checks, derived
  hubs and index, schema derived from the corpus, link resolution by slug
  (`docs/wiki-schema.md`)
- Five CI sweeps, including surface-binding and pane-registry agreement

---

## G. The UI/UX has not been rebuilt to the spec — **PARTIAL**

**Asked for:** the workspace redesign applies to the whole interface, not the
shell. The spec is the instrument aesthetic recorded in
`apps/web/src/styles/tokens.css`: square corners (`--radius: 0`), hairline
borders, no shadows, and **colour reserved for state** — green means holding,
rust means contested, and nothing else in the interface is ever coloured.

**Measured**, 43 components across `components/` and `a2ui/components/`:

- **180 violations** of that spec
- **4 need a rebuild**, 12 need revision,
  16 need touch-ups, 11 are clean

The clean ones are, without exception, the components written during the
redesign — `Board`, `Block`, `ContextBar`, `AssistantDock`, `ReadingRegion`,
`AlephLogo`, `Icons`, `CopilotChatSurface`, `ProjectWorkspace`. Everything
inherited from the previous UI still carries `rounded-lg`, `shadow-sm` and a
Tailwind palette that the token system was written to replace. `Drawers.tsx`
alone has 24 hardcoded palette colours and zero token references.

A hardcoded `text-slate-500` is not a cosmetic problem here: it does not respond
to the theme at all, so it renders the same on both grounds — which is why parts
of the interface look correct in one theme and wrong in the other.

### Per-component audit

| Component | Round | Shadow | Palette | Lines | Verdict |
|---|--:|--:|--:|--:|---|
| `Drawers` | 12 | 1 | 24 | 742 | **REBUILD** |
| `WikiPageCard` | 3 | 1 | 20 | 430 | **REBUILD** |
| `GroundingSurface` | 3 | 0 | 16 | 194 | **REBUILD** |
| `ActivityCard` | 3 | 0 | 13 | 349 | **REBUILD** |
| `HypothesesSurface` | 7 | 1 | 0 | 170 | **revise** |
| `HypothesisMatrix` | 1 | 0 | 6 | 122 | **revise** |
| `ApprovalCard` | 2 | 0 | 4 | 105 | **revise** |
| `_shared` | 2 | 2 | 2 | 222 | **revise** |
| `SourceUploadModal` | 4 | 1 | 1 | 102 | **revise** |
| `DiffCard` | 1 | 0 | 4 | 94 | **revise** |
| `FindingCard` | 0 | 0 | 4 | 65 | **revise** |
| `HtmlDocCard` | 1 | 0 | 3 | 54 | **revise** |
| `WikiBodyMarkdown` | 0 | 0 | 4 | 163 | **revise** |
| `BriefsSurface` | 1 | 0 | 2 | 50 | **revise** |
| `LeftPanel` | 3 | 0 | 0 | 175 | **revise** |
| `WikilinkChip` | 0 | 0 | 3 | 49 | **revise** |
| `ArtifactsSurface` | 2 | 0 | 0 | 207 | **touch-up** |
| `ClaimCard` | 0 | 0 | 2 | 55 | **touch-up** |
| `FormCard` | 1 | 0 | 1 | 90 | **touch-up** |
| `HtmlFrameCard` | 1 | 0 | 1 | 46 | **touch-up** |
| `ImageCard` | 1 | 0 | 1 | 38 | **touch-up** |
| `NoteEditorCard` | 2 | 0 | 0 | 116 | **touch-up** |
| `WikiSurface` | 2 | 0 | 0 | 375 | **touch-up** |
| `PipelineStrip` | 0 | 0 | 2 | 82 | **touch-up** |
| `ProjectList` | 0 | 1 | 1 | 291 | **touch-up** |
| `ChartCard` | 0 | 0 | 1 | 112 | **touch-up** |
| `NotesSurface` | 1 | 0 | 0 | 90 | **touch-up** |
| `SourceCard` | 1 | 0 | 0 | 100 | **touch-up** |
| `A2UIRightPanel` | 0 | 0 | 1 | 44 | **touch-up** |
| `Block` | 0 | 1 | 0 | 210 | **touch-up** |
| `Rail` | 0 | 1 | 0 | 122 | **touch-up** |
| `ThemeToggle` | 1 | 0 | 0 | 114 | **touch-up** |
| `ArtifactCard` | 0 | 0 | 0 | 66 | **clean** |
| `HypothesisCard` | 0 | 0 | 0 | 33 | **clean** |
| `TableCard` | 0 | 0 | 0 | 124 | **clean** |
| `AlephLogo` | 0 | 0 | 0 | 95 | **clean** |
| `AssistantDock` | 0 | 0 | 0 | 98 | **clean** |
| `Board` | 0 | 0 | 0 | 305 | **clean** |
| `ContextBar` | 0 | 0 | 0 | 139 | **clean** |
| `CopilotChatSurface` | 0 | 0 | 0 | 193 | **clean** |
| `Icons` | 0 | 0 | 0 | 151 | **clean** |
| `ProjectWorkspace` | 0 | 0 | 0 | 138 | **clean** |
| `ReadingRegion` | 0 | 0 | 0 | 110 | **clean** |

**Counting rules:** *Round* is `rounded-{sm..full}` (the spec is `--radius: 0`).
*Shadow* is `shadow-{sm..xl}` (the spec has none). *Palette* is a Tailwind
colour scale used directly instead of a token. Reproduce with the script in
this file's history, or by grepping those three patterns.

### What a rebuild means

Not a restyle. Each of the four REBUILD components predates the pane model and
holds structure that no longer fits — `Drawers.tsx` is the settings drawer that
item B1 replaces with panes, and `WikiPageCard` still carries its own layout
chrome rather than being a renderer inside a pane. Revising their colours would
leave the wrong structure wearing the right paint.

---

## Suggested order

Each step is the forcing function for the next.

1. **B2 — set the model endpoint from the UI.** Blocks using Aleph against any
   other provider, which is a stated hard requirement. Self-contained: a
   per-project endpoint and credential, a probe before saving, and discovery
   re-runs against whatever it is pointed at.

2. **C3 — the Inspector pane.** Built before E1, not after: tracing the agent
   `RUN_ERROR` by reading container logs is what makes agent bugs expensive, and
   the Inspector is the tool that makes every later item on this list cheaper to
   debug. Runs on data Aleph already has.

3. **E1 — trace the `RUN_ERROR`** with the Inspector in hand. The chat path is
   the product surface.

4. **B1 + A4 + G's four REBUILD components.** These are one piece of work, not
   three. `Drawers.tsx` is the settings drawer B1 replaces with panes; it is also
   the most drifted component in the tree (24 hardcoded colours, zero tokens);
   and it is where A4's per-plugin settings cards have to land. Rebuilding it as
   panes settles all three.

5. **G's remaining revisions.** 12 components need revision and 16 need
   touch-ups. Mechanical once the four rebuilds have set the pattern, and worth
   a sweep that fails on `rounded-*`, `shadow-*` and raw palette classes so the
   drift cannot return.

6. **A3 — catalog composition.** The other half of a plugin's UI, and it
   requires changing the single-catalog sweep's invariant.

7. **A1 — the plugin system proper.** Largest. Items 4–6 are the concrete
   forcing functions for its shape, which is why it comes after them rather
   than before.

8. **A2 — the agent authoring plugins**, once A1 exists.

9. **E2–E4**, then **D1–D2**.

**Not doing:** C1 (Enterprise Intelligence) — evaluated and declined, see above.
**Parked:** D3 and D4 until OIDC deployment is taken up as a whole.
