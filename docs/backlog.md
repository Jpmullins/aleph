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

### C1. CopilotKit Enterprise Intelligence — **PARTLY MISJUDGED; still decline**

**Correction.** My first assessment said it offered threads, inspection and
multi-tenancy, all of which Aleph has. That was incomplete — I searched the docs
and missed the product page. It also has a **self-improvement** pillar:
in-context learning from human feedback with no fine-tuning; captured in-app
interactions (clicks, edits, navigation) and approval/edit signals; learning
containers scoped per user, group or organisation; **automatic skill
development** — *"Users show the agent a task once. It learns the skill"* — plus
export as fine-tuning datasets, full auditability, and self-hosting.

That is the thing worth wanting, and it was right to keep asking about it.

**The verdict does not change, for a different reason than before.** The
deployment constraint still holds — cloud-hosted sends every interaction to
CopilotKit, self-hosting is a Helm chart into Kubernetes, and Aleph deploys with
docker compose. But the stronger argument is that **Deep Agents provides these
primitives natively, and Aleph already runs it** (`deepagents 0.6.6`). See H
below: agent-authored skills, durable skill storage, and iterate-until-good
grading are all available in the library Aleph is already built on, with no
license, no Kubernetes, and no data leaving the deployment.

Aleph also already holds the substrate the learning would run on: the action
ledger records every state mutation, and hand-edit and rejection feedback are
already captured as first-class rows.

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
- **E3. Typography and the compiled document. DIAGNOSED, and the first report
  was wrong on both counts.** Fixed 2026-08-22; the corrected diagnosis is kept
  here because the original is what a reader would otherwise act on.

  *What the original entry said, and why neither half held.* It said the
  sandboxed iframe blocks scripts, and that the page preloads a font that 404s.
  The `sandbox=""` on `apps/web/src/a2ui/components/HtmlDocCard.tsx` is
  deliberate and correct — the compiled document contains no scripts by
  construction and `apps/api/src/aleph_api/routes/wiki.py` pairs it with a
  `Content-Security-Policy: sandbox` header, so `allow-scripts` would weaken it
  for nothing. And the font path it named appears nowhere in this repository —
  neither that asset directory nor that file name is grepped anywhere in the
  tree, and the compiler emits no external reference of any kind. It was a
  browser extension's request, read as the app's.

  *The two real causes, both now fixed.*

  1. **`apps/web/index.html` loaded all three faces from `fonts.googleapis.com`**
     — two `preconnect`s and a stylesheet `<link>`. Aleph deploys as a docker
     compose stack into networks that may have no outbound route, and a CDN font
     does not fail loudly: it falls back. Every face silently became a system
     font, the interface stopped looking like itself, and nothing reported it.
     The three families are vendored now under `apps/web/public/fonts/` (12
     `woff2` files, 384,584 bytes total; 198,964 of that is the three `-latin`
     subsets an English page actually loads), declared in
     `apps/web/src/styles/fonts.css`, and the `<link>` tags are gone.
  2. **`packages/aleph-wiki/src/aleph_wiki/html_compiler.py`'s inline
     `<style>` was a second, divergent palette.** `_STYLE` hardcoded
     `background:#ffffff` with Tailwind-default slate ink and six invented
     confidence-badge colours, none of which came from
     `apps/web/src/styles/tokens.css` and none of which had a dark counterpart.
     Opened from the dark workspace the compiled page was a white rectangle in
     the middle of a near-black one; it was the only surface the design system
     did not reach, and the only place in the product with rounded corners. It
     now emits both of Aleph's palettes and switches on `prefers-color-scheme`,
     carries no `border-radius` at all, and holds a Python mirror of the two
     token blocks that `packages/aleph-wiki/tests/test_html_compiler.py` asserts
     against the stylesheet character for character.

  *What is still open.* `prefers-color-scheme` is the only theme signal that
  crosses into a `sandbox=""` iframe — measured: an embedder's `color-scheme`
  does NOT propagate to the media query in Chromium 151, tried both on `<html>`
  and on the `<iframe>` element itself. So the compiled document tracks the
  app's *default* "system" setting exactly, and diverges only for a viewer who
  has explicitly toggled the theme against their OS. Closing that needs
  `apps/api/src/aleph_api/routes/wiki.py` to accept a theme and thread it into
  the compiler, which also means a second cached `RenderedAsset` per page.
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

## H. Deep Agents capabilities Aleph is not using — **the self-improvement path**

Aleph runs `deepagents 0.6.6`. The newer releases carry most of what the plugin
and self-improvement thesis needs, and almost none of it is wired.

### What Aleph already has

Skills **are** wired: `create_deep_agent(..., skills=["/skills"])` with a
`FilesystemBackend` over `apps/api/src/aleph_api/skills/`, carrying four bundled
skills — `research`, `ach`, `wiki-style`, `report-authoring`. They follow the
[Agent Skills specification](https://agentskills.io/specification): `SKILL.md`
with `name`/`description` frontmatter, progressive disclosure in three levels
(metadata at startup, instructions on activation, resources on demand).

### Adoption table

Every Deep Agents capability, what it costs to install, and whether it runs on
the sync or async path. **Aleph's agent runs in-process inside FastAPI, so the
async column is not a preference — anything on the request path must be async or
it blocks the event loop.**

Beta and preview are noted so the risk is explicit, not so the item is skipped.
Take the best available and move when the API does.

| # | Capability | Sync / async | Install | Maturity |
|---|---|---|---|---|
| H1 | Agent-authored skills (`StoreBackend`) | **async** — `abefore_agent`, `ainvoke`; skill writes go through the store on the agent's own path | none — core `deepagents` | stable |
| H2 | Wire `aleph_kernel.skills` (AST gate + lifecycle) | **sync** to load and gate (AST parse, `exec` into a fresh namespace) at boot; **async** to mount as a kernel capability | none — already in the tree | ours |
| H3 | `RubricMiddleware` | **async** — runs inside the agent loop; the grader is a subagent | none — needs `deepagents>=0.6.5`, we have **0.6.6** | **beta** |
| H4 | `HarnessProfile` | **sync** — `register_harness_profile` at startup, before the graph is built | none — core `deepagents` | stable |
| H5 | Interpreters + dynamic subagents | **async** — executes inside the agent loop | `pip install -U "deepagents[quickjs]"` → `langchain-quickjs>=0.2.0`, Python ≥3.11 (Aleph is 3.13) | **beta** |
| H6 | Async subagents | **async by definition** — returns a job id, supervisor keeps talking; check / update / cancel | none — `deepagents>=0.5.0`; speaks Agent Protocol. **ASGI transport** when the spec omits `url`: in-process calls, no HTTP, no extra auth — needs both graphs in one `langgraph.json` | **preview** |
| H7 | `stream.subagents` (Inspector data) | **async** — `await agent.astream_events(input, version="v3")` with `asyncio.gather` to consume coordinator and subagents concurrently | none — core `deepagents` | stable |
| H8 | OpenWiki / OKF v0.1 | **sync** — offline; a format comparison, not a runtime dependency | `npm install -g openwiki` (Node CLI) — or read the OKF spec only | stable |

**Two notes that change the work.**

`langchain-quickjs` (H5) is the only new runtime dependency in the list.
Everything else is already installed.

H6's **ASGI transport** is the detail that makes async subagents viable here.
When a subagent spec omits `url`, the SDK routes through in-process function
calls instead of HTTP — no network hop, no extra auth — which fits Aleph's
in-process agent exactly. It requires both graphs registered in the same
`langgraph.json`. Local runs also need the worker pool raised: a supervisor with
three concurrent subagents needs four slots, and under-provisioning silently
queues launches rather than failing.

### H1. The agent cannot write a skill — **NOT BUILT**

This is the self-improvement mechanism, and the docs state it plainly: *"You can
also ask your agent to write a skill for a task you worked on with the agent."*

Aleph's skills backend is a **read-only host filesystem**. The agent can read
skills and can never author one, so nothing it learns in a session survives it.
`StoreBackend` gives durable cross-thread storage, which is what an authored
skill needs to persist.

The guardrail question — "what stops it removing load-bearing capability" — is
A2, and it is the same question.

### H2. `aleph_kernel.skills` is dead code — **BROKEN**

There are **two** skills implementations. The one that runs is deepagents'
`SkillsMiddleware`. The other is `packages/aleph-kernel/src/aleph_kernel/skills.py`:
`SKILL.md` plus `kernel.py`, AST-gated before execution, exec'd into a fresh
namespace so a skill cannot shadow a module, loadable as a kernel capability
with a lifecycle.

It has **no callers outside its own tests**. Verified.

That is the exact defect class CLAUDE.md names as dominant — written correctly,
read by nothing. It also matters more than most, because its AST gate is the
admission control an *agent-authored* skill would need (H1), and the capability
lifecycle is what activate/deactivate (A1) would be built on. Either wire it to
the agent path or delete it; leaving it is a third state that reads as
"we have this covered" and does not.

### H3. `RubricMiddleware` — iterate until it is actually done — **NOT BUILT**

LLM-as-a-judge at runtime: declare what "done" looks like, and the agent
self-evaluates and revises until the rubric passes or an iteration cap is hit.
Requires `deepagents>=0.6.5`; Aleph has 0.6.6, so this is available today.

Aleph's `aleph-reviewer` runs verification passes, but they are a separate stage
that reports findings, not a loop the agent closes itself. The wiki lint has
exactly the shape a rubric wants — 16 named criteria with fixes.

### H4. `HarnessProfile` — per-model harness tuning — **NOT BUILT**

Package prompt tweaks, tool-description overrides, excluded tools and middleware,
and subagent edits per provider or per model, loadable from YAML. Deep Agents
ships built-in profiles for OpenAI and Anthropic.

This matters specifically because of B2. Aleph is meant to run against any
OpenAI-compatible endpoint — litellm, ollama, vllm, bedrock — and it currently
sends **one prompt and one tool set to every model**. A 7B local model and Opus
do not want the same harness. Profiles are the built-in answer, and they pair
with the endpoint work rather than following it.

### H5. Interpreters and dynamic subagents — **NOT BUILT**

Interpreters give the agent an in-memory workspace where it writes code that
loops, branches, retries and batches — with intermediate results staying out of
model context. Dynamic subagents dispatch subagents *from that code*.

Directly relevant to **E5, the gateway rate limiting**. Today the model decides
how many subagent calls to issue, one turn at a time; the docs note this is
unreliable at scale and tends to sample rather than cover. Moving fan-out into
interpreter code makes it deterministic and bounded, which is both the fix for
unpredictable request volume and better coverage.

Requires `langchain-quickjs>=0.2.0`. Beta.

### H6. Async subagents — **NOT BUILT**

Background subagents that return a job id immediately, so the supervisor keeps
talking to the user while work proceeds — with check, mid-flight update, and
cancel. Aleph's research loop is exactly this shape and currently blocks.

Preview feature; speaks the Agent Protocol, so a self-hosted server works.

### H7. `stream.subagents` — **NOT BUILT, and it is what C3 needs**

Deep Agents projects one stream handle per delegated `task` call, each exposing
`.messages`, `.tool_calls`, `.values`, `.subagents` and `.output`, labelled by
subagent name.

That is the Inspector's data model, already available in the library. C3 does
not need to be built from raw AG-UI events.

### H8. OpenWiki and the Open Knowledge Format — **worth checking against**

LangChain ships OpenWiki, a CLI that maintains a Markdown wiki as durable agent
context, built on Deep Agents. It emits **OKF v0.1** — Markdown bundles with
front matter, indexes and linked concepts.

Aleph's wiki schema (`docs/wiki-schema.md`) was modelled on the hermes-agent
`llm-wiki` skill and converges on the same shape. If OKF is becoming a real
interchange format, aligning the frontmatter is cheap now and expensive later.
Needs a read of the OKF spec against `aleph_wiki.frontmatter` before deciding —
not a commitment yet.

---

## Suggested order

Revised after reading the current Deep Agents docs. The headline change: **the
self-improvement thesis is far closer than section A implies.** Aleph already
runs `deepagents 0.6.6` with skills wired; what is missing is a writable skills
backend and a guardrail, not a system from scratch.

1. **B2 + H4 — the model endpoint, and per-model harness profiles.** One piece
   of work. Being able to point Aleph at any OpenAI-compatible endpoint is only
   half useful while every model gets the same prompt and tool set; `HarnessProfile`
   is the built-in answer and lands with the same change.

2. **H1 + H2 + A2 — the agent can write a skill.** This is the self-improvement
   mechanism, and it is three items only because the pieces are currently
   scattered: a `StoreBackend` so an authored skill survives the session,
   `aleph_kernel.skills`' AST gate as admission control on what the agent wrote,
   and the blast-radius check that stops it removing load-bearing capability.
   H2 is dead code today; this is what makes it live, and the alternative is
   deleting it.

3. **H7 + C3 — the Inspector.** `stream.subagents` is the data model, already in
   the library. Build it before chasing E1: tracing agent failures through
   container logs is what makes every agent bug expensive.

4. **E1 — trace the `RUN_ERROR`** with the Inspector in hand.

5. **B1 + A4 + G's four rebuilds.** One piece of work. `Drawers.tsx` is
   simultaneously the settings drawer B1 replaces, the most drifted file in the
   tree, and where A4's per-plugin cards have to land.

6. **H5 — interpreters and dynamic subagents.** Also the likely fix for E5:
   model-driven fan-out is what makes request volume unpredictable, and moving
   it into interpreter code makes it bounded and deterministic.

7. **H3 — `RubricMiddleware`.** The wiki lint's 16 named criteria are already
   rubric-shaped, so there is a concrete first rubric to write.

8. **G's remaining revisions**, with a sweep that fails on `rounded-*`,
   `shadow-*` and raw palette classes so the drift cannot return.

9. **A3 — catalog composition**, then **A1 — the plugin system**. A1 last on
   purpose: items 2, 5 and 6 are the concrete forcing functions for its shape,
   and a plugin system designed before them would be designed against guesses.

10. **H6** (async subagents, for the research loop), **H8** (read the OKF spec
    against `aleph_wiki.frontmatter`), then **E2–E4** and **D1–D2**.

**Not doing:** C1 — Enterprise Intelligence. Its self-improvement pillar is real
and I was wrong to omit it, but Deep Agents provides the same primitives inside
a library Aleph already depends on, with no license, no Kubernetes, and nothing
leaving the deployment.

**Parked:** D3 and D4 until OIDC deployment is taken up as a whole.
