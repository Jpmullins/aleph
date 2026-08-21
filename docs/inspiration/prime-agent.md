# prime-agent — architecture review for Aleph's kernel

**Reviewed:** `/Users/jpmullins/Documents/code/inspiration/prime-agent` at commit `f8f0036cc` (last commit
2026-08-18, 4,533 commits). **License: MIT** (Copyright 2025 Mario Zechner; Copyright 2026 Prime
Intellect — `LICENSE:1-3`). It is a fork/rebrand of Mario Zechner's `pi` agent; the npm packages are
still published under `@earendil-works/pi-*`. MIT means ideas *and* code are legally reusable with
attribution, but per Aleph's standing constraint we reimplement, we do not vendor. Nothing here is
unsafe to copy for license reasons; the things that would be *unwise* to copy are noted in
**Worth avoiding**.

**Size:** ~150,600 lines of hand-written source (+21,891 lines of generated model metadata in
`packages/ai/src/models.generated.ts`) and **157,543 lines of tests across 427 test files** — a
roughly 1:1 test-to-source ratio.

---

## In one paragraph (plain language)

Prime Agent is a terminal coding-and-research agent. Its central bet is that **the model should write
Python, not call a menu of tools**. There is exactly one built-in tool exposed to the language model —
`ipython` — and it runs code inside a long-lived Python process (a "kernel") that keeps its variables
between turns. Everything else that would normally be a separate tool becomes something you can *call
from Python*: reading files, running shell commands, searching the web, talking to external services
(MCP servers), and even **spawning other agents** (`await rlm("review the auth flow")`). Capability is
added in two ways. **Skills** are ordinary Python packages dropped in a folder; Prime Agent installs
them into the kernel's virtual environment and the model imports them by name. **Extensions** are
TypeScript files that hook into the host program's lifecycle — they can block a dangerous command,
register an extra tool, add a `/slash` command, or repaint the terminal UI. Underneath, a background
**daemon** keeps each agent session alive in its own operating-system process, so closing your terminal
does not kill the work, and you can reattach later. The system is fast in the places where it matters
(one serialization per event fanned out to many viewers; a pre-warmed Python "fork server" that boots a
kernel in milliseconds instead of ~1.2 seconds) and it is honest that it provides **no security
sandbox** — model-written code runs as you.

Jargon defined on first use: *kernel* = the persistent Python process (a Jupyter/IPython kernel);
*ZMQ* = ZeroMQ, the message-socket library Jupyter kernels speak; *comm* = Jupyter's named
bidirectional channel between kernel code and the host; *MCP* = Model Context Protocol, a standard for
exposing external tools to agents; *RLM* = "Recursive Language Model", their name for an agent that
spawns child agents from inside its own code; *daemon* = a background process that outlives your
terminal; *COW* = copy-on-write, the OS trick where a forked child shares its parent's memory pages
until it writes to them.

---

## 0. What a user can actually DO, end to end

This is the bar Aleph must clear. The journey below is reconstructed from
`docs/quickstart.md`, `docs/usage.md`, `docs/long-running-agents.md`, `README.md` and the CLI code.

1. **Install.** `curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh`. The installer is
   a hand-written 45 KB POSIX shell script (`install.sh`) that downloads a versioned release, verifies
   a SHA-256 checksum, installs the `prime-agent` binary, and offers to prepare the Python runtime.
   There is a CI check that the installer's *rendered output* still looks right
   (`scripts/check-installer-render.mjs`, wired into `npm run check` in `package.json:23`).

2. **First launch.** `cd /path/to/project && prime-agent`. `/login` picks a provider — subscription
   OAuth (Anthropic, OpenAI Codex, GitLab Duo…) or an API key. Credentials land in one `auth.json`
   (`src/core/auth-storage.ts`, 1,126 lines, with a 1,104-line test file).

3. **First run cost.** On first use the agent builds a Python virtualenv with `uv`, installs
   `ipykernel` + `prime-agent-runtime` + default packages + every discovered Python skill in editable
   mode (`src/core/kernel/bootstrap.ts:855-919`). The UI says `› setting up python kernel (one-time,
   ~30s)…`. After that it runs offline.

4. **Working.** You type into a TUI with four regions (header / messages / editor / footer). You can:
   `@` to fuzzy-find files, Tab to complete paths, paste images, `!cmd` to run a shell command and feed
   the output to the model, `!!cmd` to run it silently, Ctrl+G to drop into `$EDITOR`. **You can queue
   messages while the agent is working**: Enter queues a *steering* message delivered after the current
   assistant turn, Alt+Enter queues a *follow-up* delivered after all work finishes, and Alt+Up/Down
   browses and edits the queue (`docs/usage.md:70-84`). This is a genuinely better interaction model
   than "wait for the agent to stop".

5. **The agent works by writing Python.** Every file read, edit, grep, shell command and API call is a
   cell in the persistent kernel. Variables survive across turns *and across compaction*:
   `config_files = list(Path(".").rglob("*.toml"))` on turn 3 is still bound on turn 40.

6. **Delegation.** From inside a cell the model writes
   `handle = await rlm("Review the public API", name="api-reviewer")`. This returns **at admission**,
   not at completion — it never blocks on the child. Children report back by *sending messages*
   (`await agent_message.send(msg, receiver_role="parent")`) or by writing files. The parent can list
   (`await rlm.list_subagents()`), follow up with, or delete children. Children survive compaction and
   kernel restarts (`docs/rlm.md:60-115`).

7. **Long-running work.** Close the terminal — the session keeps running in a detached worker process.
   `prime-agent agents` opens a browser of running/idle/saved sessions; `prime-agent attach <agent>`
   reattaches. `/goal` sets a persistent objective with a token budget the harness keeps re-prompting
   against. `/heartbeat` and `prime-agent schedule` re-enter a session on a timer or a cron expression.
   `/autonomous` continues within turn/token/time budgets and can run user-defined quality gates.
   `prime-agent send <agent> "..."` messages a running agent from any shell.

8. **Self-improvement.** `/refine` reviews the current trajectory and proposes small, evidence-backed
   edits to *supplemental* harness state — prompt notes, memories, skill descriptions, subagent specs.
   It explicitly **cannot rewrite the base system prompt** (`src/core/refinement.ts:135,451,672`), every
   applied edit stores `before`/`after` snapshots, history is an append-only JSONL, and any refinement
   can be rolled back by id (`refinement.ts:804-833`).

9. **Output.** `/export [file]` renders the session to a self-contained HTML file (template + vendored
   highlight.js in `src/core/export-html/`); `/share` uploads it as a private GitHub gist and gives you
   a link. `/usage` and `/context` break down tokens and cost for the parent **and each subagent**.

10. **Headless.** Four non-interactive modes share the same runtime: `-p` print, `--mode json`
    (event stream on stdout), `--mode rpc` (LF-delimited JSONL, 1,456 lines of protocol docs), and
    **ACP** (Agent Client Protocol — the Zed editor integration, `src/modes/acp/`). Plus an embeddable
    TypeScript SDK (`docs/sdk.md`, 1,123 lines).

**Honest verdict on the journey:** this is a complete, polished product with an unusually good
long-running-work story. What it is *not*: there is no web UI, no database, no notion of a durable
knowledge layer, no citations, no evidence model, no visualization beyond an HTML transcript export.
For a *research* workbench, "the transcript is the artifact" is the weak point — and it is exactly the
gap Aleph's claim spine is designed to fill.

---

## 1. The extension model — what IS a plugin here?

**There are three distinct plugin planes, and they are not unified.**

### Plane A — TypeScript extensions (host-side)

A plugin is **a TypeScript file that default-exports a factory function taking one object**.

The smallest complete real plugin in-tree is
`packages/coding-agent/examples/extensions/hello.ts`, in full (26 lines):

```typescript
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const helloTool = defineTool({
	name: "hello",
	label: "Hello",
	description: "A simple greeting tool",
	parameters: Type.Object({
		name: Type.String({ description: "Name to greet" }),
	}),

	async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
		return {
			content: [{ type: "text", text: `Hello, ${params.name}!` }],
			details: { greeted: params.name },
		};
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(helloTool);
}
```

Drop that file in `~/.prime/agent/extensions/`, restart (or `/reload`), and the model can call
`hello`. That is the whole ceremony.

- **Declared:** by existing as a `.ts`/`.js` file (or a directory with `index.ts`, or a directory with
  `package.json` carrying a `pi.extensions` array). `src/core/extensions/loader.ts:477-505`.
- **Discovered:** by scanning `.prime/agent/extensions/` (project) then `~/.prime/agent/extensions/`
  (global) then any `extensions` paths in settings. **One level deep only**, no recursion
  (`loader.ts:507-547`).
- **Registered:** the factory is called once with an `ExtensionAPI`; its `registerTool` /
  `registerCommand` / `registerShortcut` / `registerFlag` / `registerMessageRenderer` /
  `registerProvider` calls write into per-extension `Map`s on an `Extension` record
  (`loader.ts:168-321, 354-372`).
- **Started:** loaded via [jiti](https://github.com/unjs/jiti) so TypeScript runs with no build step
  (`loader.ts:328-349`). Factories may be `async`; startup awaits them.
- **There is no manifest, no version, no id, no declared dependencies, no capability list.**

### Plane B — Python skills (kernel-side)

A plugin is **a Python package with a `SKILL.md` and a `pyproject.toml`**.

```
web-search/
├── SKILL.md          # YAML frontmatter: name + description (Agent Skills standard)
├── pyproject.toml    # marks it Python-backed; declares dependencies
└── src/web_search/__init__.py   # must define run() to be callable
```

The smallest real one in-tree is the bundled `goal` skill —
`packages/coding-agent/skills/goal/src/goal/__init__.py`:

```python
from rlm import host_request

async def get() -> dict[str, Any]:
    """Read the current thread goal."""
    return await host_request("goal.get")
```

…with `pyproject.toml:5-10` declaring `name = "goal"`, `dependencies = []`, hatchling build backend,
and a comment noting `prime-agent-runtime` is deliberately *not* declared because it is always
pre-installed in the kernel venv.

- **Discovered:** synchronous filesystem walk over `~/.prime/agent/skills/`, `~/.agents/skills/`,
  `.prime/agent/skills/`, `.agents/skills/` walking up to the git root, package `skills/` dirs,
  settings, `--skill` flags, and finally the built-in `skills/` directory (lowest precedence).
  `src/core/skills.ts:275-385, 513-560`.
- **Installed:** `uv pip install --editable <path>` into a shared kernel venv at
  `~/.prime/agent/kernel-venv`, in **topologically sorted dependency order**
  (`src/core/kernel/bootstrap.ts:280-326`).
- **Started:** the model writes `import web_search` in a cell. That is the entire activation story.
- **Progressive disclosure:** only the frontmatter `name` + `description` go in the system prompt
  (`skills.ts:443-475`); the full `SKILL.md` is read on demand by the model. This keeps 50 skills from
  eating the context window.

### Plane C — Prime Agent packages (distribution)

A `package.json` with a `pi` key naming `extensions` / `skills` / `prompts` / `themes` directories,
installed from `npm:`, `git:`, an https/ssh URL, or a local path
(`prime-agent package install npm:@foo/bar@1.0.0`; `src/core/package-manager.ts`, 2,444 lines).
This is a *packaging* layer over planes A and B, not a fourth kind of plugin.

---

## 2. Dependencies between plugins

**The answer is split, and the split is the single most instructive thing in this codebase.**

### Plane A (TypeScript extensions): fixed extension points — the anti-pattern Aleph wants to avoid.

The `ExtensionAPI` is a **closed vocabulary**: `on(event)`, `registerTool`, `registerCommand`,
`registerShortcut`, `registerFlag`, `registerMessageRenderer`, `registerProvider`, plus ~20 action
methods (`src/core/extensions/types.ts`, 1,398 lines). The event names are a fixed union of ~30 strings
(`types.ts`, `RunnerEmitEvent`). An extension **cannot**:

- declare that it depends on another extension;
- expose a typed interface other extensions can call;
- register a new *kind* of extension point;
- register a new `host_request` type reachable from Python (that table is hardcoded in
  `AgentSession._createKernelHostHandlers()`, `src/core/agent-session.ts:8540-8620`).

The only inter-extension channel is `pi.events`, which is a bare `node:events` `EventEmitter` with
**string channels and `data: unknown`** — no schema, no discovery, no ordering, no typing:

```typescript
// packages/coding-agent/src/core/event-bus.ts:3-6
export interface EventBus {
  emit(channel: string, data: unknown): void;
  on(channel: string, handler: (data: unknown) => void): () => void;
}
```

The usage example is exactly as loose as it looks
(`examples/extensions/event-bus.ts:21-24`: `pi.events.on("my:notification", (data) => { const {
message, from } = data as { message: string; from: string }; ... })`).

Load order is *discovery order* — project dir, then global dir, then settings order
(`loader.ts:552-592`) — with no way to express "load me after X". Where order matters, the docs simply
say "handlers run in extension load order" (`docs/extensions.md`, `before_provider_request`).

Conflicts are resolved by silent precedence rules, not by a dependency graph:
- Tools: **first registration wins** among extensions (`runner.ts:368-380`), but extension tools
  **overwrite built-ins** in the final registry (`agent-session.ts:8340-8384` — `definitionRegistry.set`
  after the builtin entries). So an extension can replace `ipython` itself.
- Shortcuts: **last registration wins**, with a warning diagnostic, except for ~16 reserved
  keybindings that extensions may not override at all
  (`runner.ts:62-80, 411-450`) — the closest thing to a "protected core" in the whole system, and it
  covers only keyboard shortcuts.

### Plane B (Python skills): genuine, resolved dependencies.

Because skills are real Python packages, **plugin B can depend on plugin A by declaring it in
`[project].dependencies` and then `import a`.** The host reads those declarations, resolves them
against sibling skills, and topologically sorts the install:

```typescript
// packages/coding-agent/src/core/kernel/bootstrap.ts:302-320
const pending = new Set(pythonSkills);
while (pending.size > 0) {
  let progressed = false;
  for (const skill of [...pending].sort(...)) {
    const dependencies = dependenciesBySkill.get(skill) ?? [];
    if (dependencies.some((dependency) => pending.has(dependency))) continue;
    sorted.push(skill); pending.delete(skill); progressed = true;
  }
  if (!progressed) {
    // Cyclic local skill dependencies cannot be topologically ordered; keep a
    // deterministic order and let uv surface the packaging error if needed.
    sorted.push(...); break;
  }
}
```

Transitive sibling dependencies are pulled into the install set automatically
(`bootstrap.ts:146-161`). After install, calls between skills are **direct in-process Python function
calls with zero marshalling**. This is real composability, and it is free because they delegated it to
an existing package manager instead of inventing one.

**Lesson for Aleph:** prime-agent accidentally proves the thesis. The plane where they invented their
own extension surface (TypeScript) is the plane that cannot compose. The plane where they leaned on an
existing module system (Python packages + `uv`) composes perfectly and cost them ~200 lines of
topological sort. *A kernel whose plugins are modules in a real module system beats a kernel that
invents extension points.*

---

## 3. How plugins COMMUNICATE — the performance question

There are four distinct boundaries. Only one of them is on any hot path, and the authors worked hard
on the other three anyway.

### 3.1 Extension ↔ host: direct in-process calls, resolved once, cached

No serialization at all. `ExtensionAPI` methods close over the `Extension` record and a shared
`ExtensionRuntime` object; action methods delegate through mutable fields that `bindCore()` overwrites
with real implementations once the session exists (`loader.ts:115-161`, `runner.ts:266-330`). Event
dispatch is a nested loop over extensions and their handler arrays (`runner.ts:674-706`).

Tool references **are resolved once and cached**: `_refreshToolRegistry()` builds a
`Map<string, AgentTool>` once and stores it in `this._toolRegistry`; it is rebuilt only when an
extension calls `registerTool` after startup or on reload (`agent-session.ts:8316-8400`,
`loader.ts:182-189`). Per-call lookup is a single `Map.get`.

**Where the plugin tax is actually paid, and they did NOT avoid it:** `ExtensionRunner.emit()` calls
`this.createContext()` **unconditionally, before checking whether any handler exists**
(`runner.ts:674-676`), and `createContext()` allocates a fresh object literal with fourteen getters and
closures every time (`runner.ts:567-625`). `message_update` fires **once per streamed token**. So a
zero-extension session still allocates one 14-property object per token. It is small, but it is the
textbook shape of a plugin tax and it is trivially fixable with an early `hasHandlers()` guard (which
the class already implements at `runner.ts:486-494` and uses elsewhere).

Extension **loading** is strictly sequential — `for (const extPath of paths) { await
loadExtension(...) }` (`loader.ts:424-435`) — so one extension with a slow async factory (e.g. the
documented "fetch models from localhost:1234" pattern, `docs/extensions.md:186-215`) blocks startup for
every other extension. jiti is configured with `moduleCache: false` (`loader.ts:335`), which forces
re-evaluation on every load, including every `/reload`.

### 3.2 Host ↔ Python kernel: JSON over ZeroMQ, hand-rolled Jupyter wire protocol

`src/core/kernel/index.ts` (1,638 lines) implements the Jupyter messaging protocol directly: a
`Dealer` socket for shell, a `Subscriber` for iopub, HMAC-SHA256 signing, the `<IDS|MSG>` delimiter,
protocol version 5.3 (`index.ts:25-27, 762-768`). Every cell execution and every result is
JSON-serialized. Execution is **serialized by a promise queue** — "Serializes execute() calls — Jupyter
shell channel is request/reply" (`index.ts:604-605`).

Python code calls back into the host through a Jupyter **comm** on target `host.request`:

```python
# prime-agent-runtime/src/rlm/__init__.py:139
comm.open(data={**(payload or {}), "type": request_type})
```

with a nice detail: `request_type` is spread **last** so a payload key named `"type"` cannot reroute
the request. The host dispatches on that type against a hardcoded handler table
(`agent-session.ts:8540-8620`) and replies over the same comm. Handlers are branded with a `Symbol` +
a `WeakSet` of factory-created functions so an arbitrary function cannot be passed off as an
authorized handler (`index.ts:83-136`) — real capability-token discipline in a place most codebases
would use a plain record.

**Data across this boundary is passed by value (JSON), but the big data never crosses it.** Documents,
dataframes, parsed corpora live as Python variables in the kernel; only the `repr`/stdout crosses, and
that is capped: `DEFAULT_MAX_OUTPUT_CHARS = 65536` (`index.ts:31`). This is the key architectural
decision that makes "everything is a plugin" cheap here — **plugins compose inside one process, and
only summaries cross the boundary.**

### 3.3 Kernel startup: the fork server (the best performance idea in the repo)

Cold-booting an IPython kernel costs ~1.2s of imports. So they run a **template Python process that
pays that cost once and then `fork()`s a ready kernel per request in milliseconds**:

```python
# packages/coding-agent/src/core/kernel/fork-server-script.ts:37-50, 90-93
def _import_template():
    # Everything a kernel touches at import time. Paid once; shared COW by children.
    import IPython, ipykernel, ipykernel.kernelapp, jupyter_client, nest_asyncio
    try: import rlm
    except Exception: pass
...
    _import_template()
    # Freeze the heap so the cyclic GC doesn't write to (and thus COW-copy) the
    # shared module pages, keeping memory genuinely shared across children.
    gc.freeze()
```

The header comment is worth quoting in full: *"a long-lived template process that pays the ~1.2s
IPython/ipykernel/rlm import cost once, then forks a ready-to-run kernel per request in ~ms. Children
inherit the imported module objects via copy-on-write, bypassing the (slow, virtiofs-backed) per-file
import path"* (`fork-server-script.ts:1-8`).

The discipline around it is exemplary:
- **Every failure degrades to direct spawn.** `ForkServerUnavailable` is caught and the caller falls
  back, so "correctness never depends on fork" (`fork-server.ts:1-9`).
- **Linux only** — "fork-without-exec is unsafe on macOS" (`fork-server.ts:41-46`).
- A guard detects env vars that affect interpreter startup (`PYTHON*`, `VIRTUAL_ENV`, `CONDA_PREFIX`,
  `__PYVENV_LAUNCHER__`) and routes those kernels to direct spawn, because `sys.path` is baked in at
  import (`fork-server.ts:19-32, 104-112`).
- The template snapshots `process.env` at construction and compares against **that**, not live
  `process.env`, "so a later mutation of process.env can't make a stale template look compatible"
  (`fork-server.ts:71-96`).
- `IPKernelApp.clear_instance()` in the child, because an inherited `jupyter_client` Session
  "silently drops messages via check_pid" (`fork-server-script.ts:70-73`).

And a **measured** admission gate on top of it:

```typescript
// packages/coding-agent/src/core/kernel/boot-gate.ts:9-16
// Fork-per-child is ~ms and bypasses the FS, so we can admit well past the
// direct-spawn cap. But fork only removes the *import* cost — every admitted
// kernel still starts a live ioloop + heartbeat thread + rlm bootstrap, and
// letting all N do that at once trips the ready timeout (measured: 256 collapses
// to ~28% boot at N=200; core*4 holds 100%). So the gate stays a real bound.
```

There is a dedicated benchmark for it: `scripts/boot-bench.mjs` compares `ungated` / `gated` /
`forkserver` at N kernels and asserts forked kernels are real and namespace-isolated.

### 3.4 Worker ↔ supervisor ↔ clients: serialize once, fan out opaque bytes

The daemon's private transport is a binary frame: `4-byte JSON header length | 4-byte payload length |
small JSON routing header | opaque payload bytes` (`src/modes/session-worker/private-framing.ts:32-51`).
**The worker serializes a public event once; the supervisor parses only the routing header and forwards
the same `Buffer` to every eligible client** (`daemon-supervisor.ts:4214, 4256-4275` —
`this.writeSerialized(client, publicPayload)` inside the client loop).

Assistant streaming gets a second optimisation. Naively, `message_update` carries the whole growing
partial message, so streaming an N-token response is O(N²) bytes. They strip the accumulated `partial`
and ship only the delta:

```typescript
// packages/coding-agent/src/modes/daemon/compact-session-stream.ts:25-38
const { partial: _partial, ...assistantMessageEvent } = message.event.assistantMessageEvent as ...;
```

…and the supervisor reconstructs the public `message_update` **once** per delta before fanning it out
(`daemon-supervisor.ts:4216-4232`). Large attach snapshots stream as 512 KiB chunks through a bounded
cache, with a file-backed transcript cache above 4 MiB, and "the supervisor never constructs a
history-sized object" (`docs/daemon.md`).

**Measured, not asserted.** `test/daemon-multiclient-bench.ts` compares "legacy per-recipient
serialization/full-history attach" against "v2's compact encode-once deltas and one cached, chunked
transcript encoding" at 1/10/50/100/250 clients, reporting serialization count, serialization ms,
throughput MiB/s, wall time and sampled RSS. There are also `scripts/bench-attach-bytes.mjs`,
`scripts/bench-daemon-startup.mjs`, `test/assistant-message-streaming-bench.ts`, a CPU-profiling
harness (`scripts/profile-coding-agent-node.mjs`) and a `PI_TIMING=1` startup instrumentation module
(`src/core/timings.ts`).

### 3.5 Where it is still slow

- **MCP calls open a fresh connection per call.** The comment is admirably honest: *"Opens a fresh
  session per call: MCP sessions are not safe to hold across the kernel's snapshot/restore, and
  per-call connect keeps this robust to idle sessions and token rotation at modest latency cost"*
  (`prime-agent-runtime/src/rlm/mcp_base.py:270-274`). In a loop over 200 documents that is 200 TCP +
  TLS + MCP `initialize` handshakes.
- **Skill and resource discovery is synchronous fs** — 12 `*Sync(` calls in `skills.ts`, 20 in
  `resource-loader.ts` — walking every ancestor directory to the git root at every startup and every
  `/reload`.
- **`pyproject.toml` is parsed with regexes**, not a TOML parser (`bootstrap.ts:163-250`) — cheap, but
  it will mis-read a dependency list written in a form the regex does not anticipate.
- **Extension loading is serial** (§3.1) and **`createContext()` allocates per event** (§3.1).
- **One shell channel per kernel, serialized** — two independent long-running Python computations in
  one session cannot overlap; the parallelism story is "spawn a child agent with its own kernel".

---

## 4. Lifecycle: load, activate, deactivate, unload, reload

- **Load/activate** are the same act: call the factory. There is no separate `activate()`.
- **Deactivate/unload:** the `session_shutdown` event fires and the runtime is dropped
  (`runner.ts:180-189`). There is **no `dispose()` return value, no undo registry, no reverse-order
  unwind**. If an extension started a file watcher, opened a socket, or monkey-patched something, the
  only mechanism to undo it is its own `session_shutdown` handler. Nothing verifies it ran or
  succeeded.
- **Hot reload without restart exists** and is real: `/reload` (or `ctx.reload()`) emits
  `session_shutdown`, re-discovers and re-loads extensions/skills/prompts/themes, then emits
  `session_start { reason: "reload" }` and `resources_discover { reason: "reload" }`
  (`docs/extensions.md:1155-1180`). `pi.registerTool()` also works *after* startup — new tools appear
  to the model in the same session with no reload at all (`docs/extensions.md:1217-1222`).
- **Stale-handle invalidation is the standout safety mechanism.** After a reload or session
  replacement, every captured `pi` / `ctx` object throws on use rather than silently operating on a
  dead runtime:

  ```typescript
  // packages/coding-agent/src/core/extensions/loader.ts:145-149
  invalidate: (message) => {
      state.staleMessage ??= message ??
        "This extension ctx is stale after session replacement or reload. Do not use a captured pi or
         command ctx after ctx.newSession(), ctx.fork(), ctx.switchSession(), or ctx.reload(). …";
  },
  ```

  Every single `ExtensionAPI` method begins with `runtime.assertActive()` (`loader.ts:174-318`), and
  every `ExtensionContext` getter does the same (`runner.ts:567-625`). This is capability revocation
  by expiry, and it is cheap.
- **In-flight work during a swap:** the docs are explicit that a command handler which `await
  ctx.reload()` continues running *in the old call frame with old code*, and that "for predictable
  behavior, treat reload as terminal for that handler" (`docs/extensions.md:1173-1180`). That is a
  documented footgun, not a solved problem.
- **Kernel state survives kernel death.** `src/core/kernel/state-snapshot.ts` pickles every top-level
  name with `dill`, **per variable independently**, so one unpicklable object (socket, GPU tensor) is
  skipped and reported rather than aborting the snapshot. Caps: 256 MiB per snapshot, 16 MiB per
  variable, with oversized variables prunable on compaction. On resume the namespace is restored and
  the model is told which names came back and which failed.
- **Skill lifecycle is weaker:** adding a Python-backed skill requires a **fresh session** so kernel
  setup can install it; `/reload` only rediscovers metadata (`docs/skills.md:236`).

---

## 5. Failure and blast radius

**Load-time failure is contained per extension.** `loadExtension` wraps the whole factory call and
returns `{ extension: null, error }`; the loader collects errors and keeps going
(`loader.ts:382-397, 424-435`). One broken extension does not stop the others or the app.

**Runtime failure is contained per handler.** Every `emit*` method wraps each handler in `try/catch`
and routes the error to `emitError` → registered listeners → the UI (`runner.ts:683-706` and ~10 more
sites). Documented policy: *"Extension errors are logged, agent continues. `tool_call` errors block the
tool (fail-safe)"* (`docs/extensions.md:2493-2497`).

**But there is no isolation boundary.** Extensions run in the host process with full Node privileges.
A synchronous infinite loop, a `process.exit()`, an OOM, or an unhandled rejection in a detached
promise takes the worker down. There is no supervisor for extensions, no quarantine, no probation, no
retry, no per-extension resource budget.

**Process-level containment is real, and is where the actual blast-radius engineering lives:**
- One detached worker process per root session tree; **a worker crash affects one root tree**, and
  recovery retries at 250 ms / 1 s / 5 s before marking the root failed (`docs/daemon.md`).
- Recovery reaps the old process group and tracked detached bash trees, appends a visible recovery
  marker to the transcript, restores the root under the same active-session ID, and **does not replay
  uncertain side effects**.
- Mutating commands are keyed by `clientId + commandId` and journaled before dispatch; a command
  without a durable result is reported as **uncertain** rather than retried
  (`src/modes/daemon/command-recovery-journal.ts`).
- Session files are protected by a process-safe lease keyed by canonical path; concurrent opens return
  `session_already_active` (`src/core/session-lease.ts`).
- Backpressure is attachment-local: a blocked client stops receiving incremental events and catches up
  from its cursor; the supervisor keeps no unbounded per-client queue.
- Updates are **two-phase**: all workers checkpoint in parallel, the supervisor validates and
  atomically persists the aggregate manifest, and only then commits and stops workers. Any prepare
  failure releases everyone and all roots keep running (`docs/daemon.md`).

**Can the system refuse to remove something load-bearing? Essentially no.** There is no protected core
set, no dependency graph, no removal check. An extension may register a tool named `ipython` and
silently replace the only built-in tool (`agent-session.ts:8340-8384`); the shipped
`examples/extensions/sandbox/index.ts` does exactly this to the `bash` tool and documents it as a
feature. The only refusals in the codebase are the 16 reserved keybindings (`runner.ts:62-80`) and
`/refine`'s refusal to edit the base system prompt (`refinement.ts:672`, `"base system prompt is not
editable"`).

---

## 6. Trust and agent-authored code

**Yes, the agent can write plugins — that is a headline feature — and no, nothing inspects them.**

- The extensions doc opens with *"Prime Agent can create extensions. Ask it to build one for your use
  case."*; the skills doc and TUI doc open the same way. There is a built-in `skill-creator` skill that
  teaches the model the Agent Skills format and the Python-backed package contract, with a working
  template.
- Agent-written code is placed in the same auto-discovery directories as human-written code and loads
  identically on next launch (or on `/reload` for extensions).
- **There is no gate, no AST inspection, no permission declaration, no signing, no capability
  manifest.** The word "sandbox" appears in the codebase mainly as a *warning that there isn't one*:

  > *"Prime Agent executes model-generated Python and project commands with your user permissions. Its
  > worker and kernel processes improve lifecycle isolation and recovery; they are **not** a security
  > sandbox."* (`README.md`)

  > *"Workers and kernels are separate processes for lifecycle and failure containment, not security
  > sandboxes. They normally run with the same operating-system permissions as the client."*
  > (`docs/architecture.md`)

- The mitigations are **social and opt-in**: "review changes and use trusted repositories,
  instructions, skills, and extensions only"; run untrusted work in an external sandbox. Security
  warnings are repeated at every plugin-installation surface (`docs/extensions.md:110`,
  `docs/skills.md:26`, `docs/packages.md:20`).
- OS-level sandboxing exists only as an **example extension** you must install yourself:
  `examples/extensions/sandbox/` wraps `@anthropic-ai/sandbox-runtime` (sandbox-exec on macOS,
  bubblewrap on Linux) with a config file for allowed domains and read/write path denies — and it works
  by *overriding the built-in bash tool*, which is itself the un-gated override mechanism.
- The one genuine trust mechanism is the **host-request handler brand**: only handlers minted by
  `createHostRequestHandler` (tracked in a `WeakSet`, marked with a private `Symbol`) can receive
  dispatcher authority, and the per-call `HostRequestContext` carries a `requestId`, a `generation`,
  an `AbortSignal` and an `isCurrent()` predicate so a handler can reject work after its authority is
  revoked (`src/core/kernel/index.ts:66-142`). That is a well-built capability token — applied to
  exactly one boundary.
- `/refine` (the self-modification path) *is* bounded: supplemental state only, base prompt immutable,
  before/after snapshots per edit, append-only history, rollback by id, and a scope rule that a local
  refinement may never edit global entries (`refinement.ts:135-151, 707-833`).

---

## 7. State and shared services

**Extensions reach services through a context object, not globals or DI.** `ExtensionContext`
(`types.ts:281-310`) hands over `ui`, `hasUI`, `cwd`, a **read-only** `sessionManager`, the
`modelRegistry`, the current `model`, `signal`, and methods `isIdle`, `abort`, `hasPendingMessages`,
`shutdown`, `getContextUsage`, `compact`, `getSystemPrompt`. Command handlers get a strictly larger
`ExtensionCommandContext` adding `waitForIdle`, `newSession`, `fork`, `navigateTree`, `switchSession`,
`reload` (`types.ts:316-360`) — a deliberate two-tier authority split: **tools cannot restart the
session; commands can.** The docs make the split explicit ("Tools run with `ExtensionContext`, so they
cannot call `ctx.reload()` directly").

Beyond that, **everything is reachable by everything**. `ExtensionAPI.exec()` runs arbitrary commands;
Node built-ins and any npm package are importable; there is no scoping, no per-extension capability
grant, no audit trail of what an extension touched. `sessionManager` is read-only, but persistence is
available anyway via `pi.appendEntry()`.

The recommended state pattern is unusual and worth noting: **store extension state in tool-result
`details`, and rebuild it by replaying the session branch on `session_start`**
(`docs/extensions.md:1627-1660`). That makes extension state automatically correct under `/fork`,
`/tree` navigation and session branching — the transcript is the source of truth. It is the same
instinct as an event-sourced ledger, applied to plugin state.

Python skills reach shared services through exactly one door — `await rlm.host_request(type, payload)`
— and the host owns every state transition. `docs/rlm.md` states the boundary plainly: *"This keeps
credentials, provider execution, transcript writes, worker routing, and scheduling out of Python while
retaining a programmatic model interface."*

---

## 8. Concurrency model

- **Host: single-threaded Node with async/await.** No worker threads in the agent path.
- **Isolation is by process:** one detached OS process per root session tree, one catalog subprocess
  for saved-session scans, one IPython kernel process per session (plus per RLM child), one fork-server
  template per Python interpreter.
- **Tool calls within one assistant message run in parallel by default.** Preflight (validation +
  `tool_call` interception) runs **sequentially** in assistant source order, then execution is
  `Promise.all` over the prepared calls; `tool_execution_end` fires in completion order but the
  tool-result *messages* are emitted in source order so the transcript stays deterministic
  (`packages/agent/src/agent-loop.ts:667-720`). Mode is switchable: `"sequential" | "parallel"`
  (`packages/agent/src/types.ts:29-37`).
- **Shared-state corruption is prevented by a per-file promise queue**, keyed by
  `realpathSync.native()` so symlink aliases share one queue:

  ```typescript
  // packages/coding-agent/src/core/tools/file-mutation-queue.ts:19-38
  export async function withFileMutationQueue<T>(filePath: string, fn: () => Promise<T>): Promise<T> {
    const key = getMutationQueueKey(filePath);
    const currentQueue = fileMutationQueues.get(key) ?? Promise.resolve();
    ...
  }
  ```

  The docs spell out the exact lost-update failure this prevents and instruct custom tools to opt in
  (`docs/extensions.md:1690-1720`). Opt-in, not enforced.
- **Kernel execution is serialized** per kernel (`index.ts:604-605`), with an interrupt path and a
  `KernelBusyAfterInterruptError` for a cell that will not die.
- **Kernel boots are admission-controlled** by a semaphore whose bound was measured, not guessed
  (`boot-gate.ts`, §3.3).
- **Agent-level parallelism is `rlm(...)`:** spawn N children, each with its own session, context
  window, and optionally its own kernel; they report back by message. The call returns at admission so
  the parent never blocks.

---

## 9. What an agent/tool actually is, and how tools reach the model

**An agent** is an `AgentSession` (`src/core/agent-session.ts`, **10,963 lines** — the god object of
this codebase) owning provider calls, the prompt queue, tools, compaction, goals, child lifecycles and
transcript writes. Sessions form a tree: one root per worker, RLM children beneath it. A session is
persisted as a flat JSONL transcript plus an artifact directory (scheduled jobs, kernel snapshot,
harness state).

**A tool** is a `ToolDefinition`: `{ name, label, description, parameters: TSchema, execute(toolCallId,
params, signal, onUpdate, ctx), prepareArguments?, promptSnippet?, promptGuidelines?, renderCall?,
renderResult? }` (`types.ts`, `docs/extensions.md:1703-1830`). Schemas are **typebox** (`Type.Object`),
converted to provider JSON Schema in `packages/ai`. `onUpdate` streams partial results into the TUI;
`renderCall`/`renderResult` let a tool draw its own transcript rendering.

**Exposure to the model is deliberately minimal.** By default there is **exactly one** tool:

```typescript
// packages/coding-agent/src/core/tools/index.ts:45-55
export type ToolName = "ipython";
export function createAllToolDefinitions(cwd: string, options?: ToolsOptions): Record<ToolName, ToolDef> {
  return { ipython: createIpythonToolDefinition(cwd, options?.ipython) };
}
```

`bash` and `edit` exist as constructors (`createBashTool`, `createEditTool`) for extensions and
embedders, but are not in the default model-facing set. The system prompt does **not** render a tool
list — it relies on provider tool schemas — and `promptGuidelines` bullets are appended only while a
tool is active (`docs/extensions.md:1661-1700`).

The consequence is the most important context-economics decision in the system: **capability does not
consume context.** 50 Python skills cost ~50 one-line descriptions. An MCP server with 40 tools costs
**zero** tool schemas, because MCP integrations are exposed as Python objects whose methods are bound
lazily by `__getattr__`, with the JSON Schema attached as the method's `__doc__` so the model can
`help()` it on demand:

```python
# prime-agent-runtime/src/rlm/mcp_base.py:290-310
def __getattr__(self, name: str):
    if name.startswith("_"): raise AttributeError(name)
    async def _call(**kwargs): 
        await self._ensure_tools()
        ...
        return await self.call_tool(name, kwargs)
    _call.__doc__ = f"{desc}\n\nArguments (JSON Schema):\n{json.dumps(schema, indent=2)}"
    return _call
```

Compare: harnesses that expose every MCP tool as a model tool spend thousands of tokens per turn on
schemas the model will not use.

Model discovery, by contrast, is the weak spot: `packages/ai/src/models.generated.ts` is a **21,891-line
checked-in list** of models, prices, context windows and capability flags, regenerated by
`packages/ai/scripts/generate-models.ts` from models.dev, OpenRouter and hand-written per-provider
blocks. Extensions can add providers at runtime via `pi.registerProvider()` (including the documented
async "fetch /v1/models from a local server" pattern), but the default posture is a shipped catalogue.
**Aleph's gateway-driven discovery with `pricing_source` provenance is strictly better here.**

---

## 10. The single best idea, and the single worst

### Best: one tool — the model programs, and capability composes inside the program.

By exposing `ipython` and nothing else, prime-agent moves plugin composition from the *tool-call
boundary* (serialized, one round-trip per call, schema in context, one call per model turn) to the
*Python function-call boundary* (in-process, nanoseconds, zero context cost, arbitrary control flow).
Skills call skills. A skill can spawn a subagent. A loop over 500 files is one cell, not 500 tool
calls. The context window holds capability *descriptions*, never capability *schemas*. This is the
answer to "how do I make a plugin system that runs as fast as a single compiled program": **make the
plugin boundary a module import inside one process, and make the expensive boundary (host ↔ model) as
narrow as possible.**

The runner-up is the kernel fork server (§3.3) — one measured trick that turns a 1.2 s cold start into
milliseconds, with a fallback path so correctness never depends on it.

### Worst: two extension systems with opposite composability, and no gate on either.

The TypeScript plane is a closed set of ~30 event names and 7 registration methods with an untyped
`EventEmitter` as the only inter-plugin channel — precisely the VSCode-style fixed-surface model, and
extensions cannot depend on, call, or extend each other. Meanwhile the Python plane composes perfectly.
An author who wants to add a capability must guess which plane it belongs to, and a capability that
needs *both* (say, a research connector that needs a kernel API *and* a permission gate *and* a new
host-request type) cannot be written at all without patching `agent-session.ts` — the 10,963-line god
object whose hardcoded handler table is the real extension registry. Layered on top: **anything can
replace anything** (an extension may override `ipython` itself), agent-authored plugins are loaded
with no inspection whatsoever, and there is no notion of a protected core.

---

## Worth stealing for Aleph

1. **Make the primary agent surface a persistent programmable runtime, not a tool menu.**
   *(`src/core/tools/index.ts:45-55`, `docs/rlm.md`)* Aleph already has a sandboxed code runner; the
   move is to make it **persistent and stateful across turns** and to expose the belief/RKS services as
   importable modules inside it. Retrieval over 45 pairs becomes one cell, not 45 tool calls. The
   plugin tax disappears because plugins call each other with a Python function call. This is also the
   direct answer to the owner's speed worry.

2. **Let capability compose through a real module system, and just topologically sort it.**
   *(`src/core/kernel/bootstrap.ts:280-326, 146-161`)* ~200 lines bought prime-agent genuine plugin→
   plugin dependency. Aleph's `aleph-kernel` already computes a dependency graph; wire it to a
   *package* identity (uv workspace member / entry-point group) rather than to a bespoke capability id,
   and plugin-to-plugin calls cost nothing.

3. **Progressive disclosure of capability.** *(`src/core/skills.ts:443-475`, `docs/skills.md:159-166`)*
   Only `name` + `description` in the system prompt; full instructions loaded on demand by the agent.
   With MCP-as-Python-object (`mcp_base.py:290-310`), an external server with 40 tools costs zero
   context. Aleph should adopt both: **capability count must not scale context cost.**

4. **Fan-out discipline: serialize once, forward opaque bytes; ship deltas, not growing state.**
   *(`src/modes/session-worker/private-framing.ts:32-51`,
   `src/modes/daemon/daemon-supervisor.ts:4214,4256-4275`,
   `src/modes/daemon/compact-session-stream.ts:25-38`)* Aleph's `SurfaceStreamProvider` multiplexes one
   SSE connection per reading region — same instinct. The upgrade is (a) encode each surface event once
   at the producer and hand the same buffer to every subscriber, and (b) never re-send an accumulating
   partial. Prime-agent measured this: `test/daemon-multiclient-bench.ts` at 1–250 clients.

5. **Pre-warmed process templates with a mandatory fallback and a measured admission gate.**
   *(`src/core/kernel/fork-server-script.ts`, `fork-server.ts:1-9,41-46`, `boot-gate.ts:9-16`)* If
   Aleph's code runner or a per-project worker pays a cold-import cost, fork it from a `gc.freeze()`-d
   template. The two rules that make it safe: **every failure path degrades to the slow path**, and
   **the concurrency bound is a measured number with the measurement in the comment.**

6. **Stale-handle invalidation as capability revocation.**
   *(`src/core/extensions/loader.ts:115-161`, `runner.ts:567-625`)* After a hot swap, every captured
   handle throws with a message that names the mistake and the fix. Aleph's kernel already unwinds
   effects LIFO; adding "and every capability handle minted before the swap now throws" closes the
   window where a plugin keeps writing through a dead reference. Pair it with the host-request
   `Symbol` + `WeakSet` brand (`src/core/kernel/index.ts:83-142`) so a forged handler cannot be
   substituted.

7. **Bounded self-modification with snapshots and rollback.** *(`src/core/refinement.ts:135-151,
   707-833`)* `/refine` may only write *supplemental* state, never the base prompt; every edit records
   `before`/`after`; history is append-only JSONL; any refinement is revertible by id. This is the
   shape Aleph's "agent authors plugins for itself" needs — and Aleph can do it better by putting the
   refinement history in the `ActionLedgerEvent` chain instead of a side file.

8. **Extension state stored in the transcript and replayed on start.**
   *(`docs/extensions.md:1627-1660`)* Plugin state derived from an append-only log is automatically
   correct under fork, branch and revert. Aleph already has the ledger; make it the substrate for
   plugin state rather than letting plugins own private mutable state.

9. **A ledger for uncertainty, not just for success.** *(`docs/daemon.md`,
   `src/modes/daemon/command-recovery-journal.ts`)* Mutations are journaled **before** dispatch keyed
   by `clientId + commandId`; a command with no durable result is reported as **uncertain and never
   replayed**. Aleph's ledger records what happened; borrowing "record intent before the effect, and
   have an explicit *uncertain* terminal state" makes crash recovery honest.

10. **Two-tier authority on the plugin context.** *(`types.ts:281-360`)* Tools get a narrow context;
    only user-initiated commands get session-replacement powers. A cheap, legible privilege split that
    Aleph can mirror as agent-context vs. operator-context capabilities.

11. **Per-file mutation queue keyed by `realpath`.** *(`src/core/tools/file-mutation-queue.ts:19-38`)*
    Parallel tool execution makes lost updates inevitable. Aleph should make the equivalent
    **mandatory** (not opt-in) for any capability that mutates a shared resource.

12. **Build the performance culture, not just the optimisation.** Benchmarks in-tree
    (`test/daemon-multiclient-bench.ts`, `scripts/boot-bench.mjs`, `scripts/bench-attach-bytes.mjs`,
    `scripts/bench-daemon-startup.mjs`), a CPU-profiling harness, `PI_TIMING=1` startup
    instrumentation, and measured constants quoted in the comment that sets them. Aleph has
    `scripts/acceptance.sh` and a retrieval eval; a `scripts/bench.sh` with the same "the number is in
    the comment" discipline would carry the composability claim.

---

## Worth avoiding

1. **Do not build two extension planes with different composability rules.** Aleph must have exactly
   one plugin identity. If a plugin needs both a host-side hook and an in-runtime module, that must be
   two faces of one declaration, not two systems.

2. **Do not let the extension API be a closed list of event names and `register*` methods.** That is
   the fixed-surface trap. Prefer: a plugin exports a typed interface; other plugins resolve it through
   the kernel by capability id; the kernel enforces the declared dependency. Untyped
   `EventEmitter`-with-string-channels (`event-bus.ts:3-6`) is not composability, it is a shared global
   namespace with no contract.

3. **Do not put the real extension registry inside a 10,963-line god object.** The hardcoded
   `_createKernelHostHandlers()` table (`agent-session.ts:8540-8620`) is the true plugin surface for
   the Python plane, and no plugin can add to it. Aleph's kernel capability declarations must be the
   only registry, and adding one must not require editing a session class.

4. **Do not let anything silently override anything.** "First registration wins" for extension tools
   but "extension tools overwrite built-ins" (`agent-session.ts:8340-8384`) is a rule nobody can hold
   in their head. Aleph's protected core set + refusal-to-remove is the right answer; keep it, make it
   reachable, and extend it from "load-bearing capability" to "load-bearing *tool name*".

5. **Do not ship a model catalogue.** `models.generated.ts` is 21,891 checked-in lines that go stale
   the day they are generated. Aleph's gateway-driven discovery with `pricing_source` provenance is
   already better — do not regress toward a bundled list.

6. **Do not accept "we warn the user" as the trust model for agent-authored code.** Prime-agent's own
   docs concede it has no sandbox, and its OS-level sandboxing ships as an *example extension* that
   works by overriding a built-in tool. Aleph already has an AST gate, a spawn ledger with probation,
   and a credential-less network-partitioned runner — that is the differentiator. Make it *reachable*
   (the audit found the agent-facing half unreachable) rather than adding more surface.

7. **Do not allocate per-event context objects before checking for subscribers.**
   (`runner.ts:674-676` + `createContext()` at `runner.ts:567-625`, on a per-token event.) The class
   already has `hasHandlers()`. Cheap fix, but the general rule matters more: **on any per-token or
   per-chunk path, no allocation unless someone is listening.**

8. **Do not load plugins serially at startup** (`loader.ts:424-435`) or **walk the filesystem
   synchronously to discover them** (12 `*Sync(` in `skills.ts`, 20 in `resource-loader.ts`). Discovery
   should be a cached manifest invalidated by mtime, and independent plugin inits should be
   concurrent.

9. **Do not parse structured config with regexes.** `bootstrap.ts:163-250` regex-parses
   `pyproject.toml` to find `[project].dependencies`. Use a real parser; Aleph already reads
   `aleph.toml` properly.

10. **Do not leave hot-reload semantics as documented footguns.** "Code after `await ctx.reload()`
    still runs from the pre-reload version… treat reload as terminal for that handler"
    (`docs/extensions.md:1173-1180`) is a spec written around a race. Aleph's revertible-effect kernel
    can do better: quiesce in-flight work, unwind, swap, resume — and refuse the swap if in-flight work
    cannot be quiesced.

11. **Do not open a connection per external call.** `mcp_base.py:270-274` opens a fresh MCP session per
    tool call *by design*, for robustness reasons that are real but that a connection pool with health
    checks solves better. Aleph's connectors should pool.

12. **Do not stop at "the transcript is the artifact."** Prime-agent's research output is an exported
    HTML transcript. That is exactly where Aleph's claim spine — durable claims, verbatim-anchored
    evidence, typed edges, derived confidence, prose *rendered from* the layer — beats it outright.
    This is the "something the owner cares about" that Aleph should win on, and it is not a
    performance argument at all.
