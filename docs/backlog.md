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

### C1. CopilotKit Intelligence — **NOT BUILT**

Asked about explicitly; never adopted, never evaluated in code.

### C2. MCP Apps — **NOT BUILT**

The third band. Would let an MCP server ship its own UI, which is close to what
A3 wants for plugins.

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

## Suggested order

1. **B2** — set the model endpoint from the UI. Blocks using Aleph against any
   other provider, which is a stated hard requirement.
2. **E1** — trace the agent `RUN_ERROR`. The chat path is the product surface.
3. **B1 + A4** — settings as panes, with a per-plugin settings contract. B1 is
   the container A4 needs.
4. **A3** — catalog composition, which is the other half of a plugin's UI.
5. **A1** — the plugin system proper. Largest, and the three above are the
   concrete forcing functions for its shape.
6. **A2** — the agent authoring plugins, once A1 exists.
7. **E2–E4**, then **D1–D2**.

**D3/D4** stay parked until OIDC deployment is taken up as a whole.
