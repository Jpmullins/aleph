# hermes-agent — architecture review for Aleph's kernel

**Repository reviewed:** `/Users/jpmullins/Documents/code/inspiration/hermes-agent`
(NousResearch/hermes-agent, HEAD dated 2026-08-19, 23,858 commits)
**License:** MIT (`LICENSE`, "Copyright (c) 2025 Nous Research"). Permissive — safe to *read and
reimplement*. Aleph's standing rule (reimplement, never vendor) has no license obstacle here, but
also no license excuse to copy: see **Licensing note** at the end.
**Reviewed read-only.** Nothing under `inspiration/` was modified.

---

## In one paragraph (plain language)

Hermes is a personal AI agent you talk to from a terminal or from Telegram/Discord/Slack. Its
distinctive bet is **the agent gets better at your work by writing prose for itself, not code for
itself**: after a hard task it writes a *skill* — a Markdown file describing how to do that kind of
task — and it curates a memory file about you. Code extensions ("plugins") exist too, but only
*humans* write those. A plugin is a folder with a small YAML description file (`plugin.yaml`) and a
Python file with one function, `register(ctx)`. When Hermes starts, it scans four folders, reads the
YAML, imports the Python, and calls `register(ctx)`; the plugin uses the `ctx` object to hand the
host callbacks — a new tool the model can call, a "run me before every tool call" hook, a chat-platform
adapter. Everything a plugin registers is recorded in an **ownership ledger** with the exact undo
operation next to it, so unloading the plugin runs those undos in reverse and, when two plugins
claimed the same slot, restores the right earlier one. Plugins run **in the same Python process** as
the agent, so a plugin call is an ordinary function call — no serialization, no IPC — and Hermes is
explicit that this is *not* a security sandbox. The performance work is real and measured, but it is
aimed at two costs Aleph should care about: **process startup latency** (avoid importing a plugin's
code until something actually uses it) and **tokens in the model's context window** (which is the
expensive resource in an agent, far more than CPU).

---

## 0. How big is the real, hand-written core?

This is the largest of the five inspiration codebases, and the surprising answer is that **almost
none of it is vendored or generated.** There is no checked-in `node_modules`, no `site-packages`, no
generated-code markers (`grep -rl "@generated\|AUTO-GENERATED\|DO NOT EDIT"` returns nothing across
`.py`/`.ts`). What is there is genuinely hand-written (very evidently LLM-assisted — comments cite
GitHub issue numbers like `#64165`, `#78050` on almost every design decision).

| Bucket | Lines | Files |
|---|---:|---:|
| Python, **excluding** `tests/` | **936,271** | 1,209 |
| Python tests (`tests/`) | 804,712 | 3,168 |
| TypeScript/TSX, desktop app (`apps/desktop`) | 386,437 | ~1,500 |
| TypeScript/TSX, everything else (`ui-tui`, `web`, `website`) | 155,568 | — |
| Markdown (docs, `skills/`, website content) | 469,577 | — |

So the "~2.09M lines" figure is real code, not vendor noise. The **agent core you would actually
learn from** is the ~936k lines of Python, and even that is dominated by a handful of monoliths:

```
20,767  cli.py
13,086  hermes_state.py
 9,053  run_agent.py
 2,493  hermes_state_search.py
 8,182  tools/mcp_tool.py
```

**The extension system proper is ~17,400 lines across 15 files** — that is the part worth reading:

| File | Lines | What it is |
|---|---:|---|
| `hermes_cli/plugins.py` | 6,561 | discovery, manifest, `PluginContext`, `PluginManager`, event bus, hooks |
| `hermes_cli/plugins_cmd.py` | 3,159 | `hermes plugins {install,enable,disable,doctor,…}` CLI |
| `tools/registry.py` | 1,309 | the tool registry plugins register into |
| `agent/plugin_llm.py` | 1,217 | host-owned LLM facade handed to plugins |
| `hermes_cli/plugin_packs.py` | 762 | plugin bundles |
| `gateway/platform_registry.py` | 698 | chat-platform adapter registry |
| `hermes_cli/agent_plugins.py` | 571 | "portable" (non-Python) plugin format |
| `hermes_cli/plugin_capabilities.py` | 393 | declared capabilities + consent hashing |
| `hermes_cli/plugin_dev.py` | 365 | `hermes plugins doctor` — load a plugin in a disposable runtime |
| `tools/plugin_guard.py` | 342 | install-time static security scan |
| `hermes_cli/plugin_index.py` | 305 | plugin catalog/search |
| `registration_lifecycle.py` | 128 | **the replacement-lease coordinator — the best file in the repo** |
| `hermes_cli/middleware.py` | 327 | request/execution middleware contract |
| `agent/plugin_stream_hooks.py` | 176 | off-hot-path streaming observers |
| `plugins/plugin_storage.py` + `plugin_utils.py` | 215 | plugin-scoped durable state helpers |

Bundled plugins are another 133,270 lines under `plugins/`, but ~60% of that is five chat-platform
adapters (Telegram 10,886; Discord 10,557; Slack 9,612; Feishu 5,896; Matrix 5,462) — application
code, not architecture.

---

## 1. The extension model

### There are two, and the split is the whole story

| | **Plugin** | **Skill** |
|---|---|---|
| What it is | directory + `plugin.yaml` + `__init__.py` with `register(ctx)` | directory + `SKILL.md` (YAML frontmatter + prose) |
| Who writes it | a human | **the agent, for itself** |
| Runs where | in-process Python, fully privileged | it doesn't "run" — it is loaded into the model's context |
| Installed by | `hermes plugins install owner/repo` (human CLI only) | `skill_manage` tool (the model calls it) |
| Reviewed by | `tools/plugin_guard.py` static scan at install | `tools/skills_guard.py` scan + a linter |

There is **no agent-facing tool that installs, enables, or disables a plugin.** The tool inventory
under `tools/` has `skill_manage`, `skills_list`, `skill_view` — and nothing for plugins;
`hermes_cli/plugins_cmd.py:951-2216` (`cmd_install`, `cmd_enable`, `cmd_disable`, `cmd_remove`) is
CLI-only. Hermes's self-improvement loop is **documents all the way down.**

### A plugin, concretely

Four discovery sources, later sources override earlier ones on name collision
(`hermes_cli/plugins.py:1-32`):

1. bundled — `<repo>/plugins/<name>/`
2. user — `~/.hermes/plugins/<name>/`
3. project — `./.hermes/plugins/<name>/` (opt-in via `HERMES_ENABLE_PROJECT_PLUGINS`)
4. pip — packages exposing the `hermes_agent.plugins` entry-point group
   (`hermes_cli/plugins.py:400`, `ENTRY_POINTS_GROUP`)

Directory plugins are imported under a synthetic namespace `hermes_plugins.<slug>`
(`_NS_PARENT`, `hermes_cli/plugins.py:540`) via `importlib.util.spec_from_file_location`
(`hermes_cli/plugins.py:4954-5020`), which is what makes profile-scoped reload possible.

**Smallest complete real plugin** — `plugins/disk-cleanup/`:

```yaml
# plugins/disk-cleanup/plugin.yaml   (the whole manifest)
name: disk-cleanup
version: 2.0.0
description: "Auto-track and clean up ephemeral files ... Runs via plugin hooks — no agent action required."
author: "@LVT382009 (original), NousResearch (plugin port)"
hooks:
  - post_tool_call
  - on_session_end
```

```python
# plugins/disk-cleanup/__init__.py:309-316   (the whole registration)
def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "disk-cleanup",
        handler=_handle_slash,
        description="Track and clean up ephemeral Hermes session files.",
    )
```

That is the entire contract: a YAML file the host can read **without importing anything**, and one
function that receives a capability-bearing context object.

**Manifest v2** (`hermes_cli/plugins.py:1031-1108`, parsed at `:683`) adds `manifest_version`,
`api_version`, `requires_plugins`, `python_dependencies` (validated and surfaced, **never
auto-installed**), `config_schema`, `license`, `homepage`, `tags`, `emits`, `listens`. Every v2 field
is *advisory*: a bad value warns, it never fails the load (`:687-689` "Every problem is a warning,
never a load failure — v2 metadata is advisory and additive").

---

## 2. Dependencies between plugins

**Both models are present, and the fixed-extension-point one dominates in practice.**

### The good half: real declared dependencies with a topological load order

```python
# hermes_cli/plugins.py:854-921
def resolve_plugin_load_order(manifests) -> List[str]:
    """When A requires B, B sorts before A (so B's ``register()`` runs first).
    Ties break alphabetically for determinism. Dependency cycles are detected,
    warned about, and ... fall back to alphabetical order..."""
    import graphlib
    ...
    sorter = graphlib.TopologicalSorter(edges)
    try:
        sorter.prepare()
    except graphlib.CycleError as exc:
        logger.warning("Plugin dependency cycle detected (%s); falling back to alphabetical", ...)
        return keys
```

and a runtime probe:

```python
# hermes_cli/plugins.py:1406-1418
def has_plugin(self, plugin_id: str) -> bool:
    """Companion to the advisory ``requires_plugins`` manifest field: a
    missing dependency never blocks load, so plugins probe availability
    at runtime with this."""
```

Plus a namespaced plugin-to-plugin **event bus** (§3) so B can talk to A without importing it.

**But**: `grep -rn requires_plugins --include='*.yaml'` over the whole tree returns **zero shipped
plugins using it.** The mechanism exists and is unexercised. And it is *ordering-only* — a missing
or disabled dependency logs a warning and loads the dependent anyway
(`hermes_cli/plugins.py:894-902`).

### The bad half: 37 fixed hooks and 23 fixed registration verbs

`VALID_HOOKS` (`hermes_cli/plugins.py:156-395`) is a hard-coded set of **37** event names.
`PluginContext` exposes **23** distinct `register_*` methods:

```
register_approval_transport   register_auxiliary_task     register_browser_provider
register_cli_command          register_command            register_context_engine
register_context_reference    register_dashboard_auth_provider
register_hook                 register_image_gen_provider register_memory_provider
register_middleware           register_platform           register_redaction_patterns
register_secret_source        register_skill              register_slack_action_handler
register_system_prompt_section register_tool              register_transcription_provider
register_tts_provider         register_video_gen_provider register_web_search_provider
```

This is exactly the VSCode-shaped model Aleph says it wants to avoid. Every new capability
*category* — TTS, video generation, browser, dashboard auth, secret sources — required a **core
patch**: a new registry module, a new `register_X_provider` method, a new tracked-registration kind.
And those methods are near-verbatim copies of each other; compare
`register_video_gen_provider` (`:2428-2472`) with `register_tts_provider` (`:2648-2706`) — same
five-step shape (`snapshot → register → verify identity → _track_replacement(slot, restore) → log`),
different imports.

**Verdict for Aleph: extension points here are host-owned surfaces, not composable capability.**
Plugin B can depend on plugin A in principle (manifest edge + `has_plugin` + event bus), but the
shipped ecosystem does not, because the interesting composition all happens through host-defined
slots that only the host can add.

---

## 3. How plugins communicate — the performance question

This is the section the owner's worry maps to, so it gets the most detail.

### 3.1 The base case: direct in-process calls, nothing serialized

A plugin-provided tool is an ordinary Python callable stored in a dict and invoked directly:

```python
# tools/registry.py:1102-1140
def dispatch(self, name, args, *, scope=None, **kwargs) -> str | dict:
    entry = self.get_entry(name, scope=scope)
    if not entry:
        return tool_error(f"Unknown tool: {name}")
    try:
        if entry.is_async:
            from model_tools import _run_async
            result = _run_async(entry.handler(args, **kwargs))
        else:
            result = entry.handler(args, **kwargs)
        return self._normalize_handler_result(name, result)
    except Exception as e:
        ...
        return tool_error(sanitized)
```

No serialization, no marshalling, no proxy. Handlers are resolved by name at call time, not cached
per call site. The **only** result-shape constraint is `_normalize_handler_result`
(`tools/registry.py:1072-1100`): a tool must return a `str`, or one specific multimodal envelope
`{"_multimodal": True, "content": [...]}`. Anything else is converted into an error. So the wire
format between host and plugin is "a Python string" — cheap, and deliberately so.

Hooks are the same: a plain loop over callables (`hermes_cli/plugins.py:5071-5115`).

**Do plugins pass big data across the boundary?** Mostly no, but by convention rather than by
design. Tool results are strings, and there is a separate size-budget system
(`tools/tool_output_limits.py`, `registry.get_max_result_size`, `tools/tool_result_storage.py`) that
spills oversized results to disk and hands back a reference. Retrieved documents live in SQLite or on
disk; the plugin receives paths/ids.

### 3.2 Where the plugin tax gets paid, and how each site avoids it

**(a) Zero-plugin cost must be zero.** Every fire site gates on `has_hook()` before building a
payload:

```python
# hermes_cli/plugins.py:285-288 (comment on the kanban hooks)
# Cost rule: every call site short-circuits on has_hook(), so when nothing
# subscribes no payload is built and the hot paths (each dispatcher tick,
# each task write) pay one dict probe.
```

Real call sites: `model_tools.py:1165`, `agent/conversation_loop.py:2878,6578,8057`,
`tools/transcription_tools.py:1412`, `gateway/run.py:15446`, `hermes_cli/kanban_db.py:226`. Middleware
does the same, and skips the defensive deep-copy entirely when nothing is registered:

```python
# hermes_cli/middleware.py:86-92
if not _has_middleware(LLM_REQUEST_MIDDLEWARE):
    return RequestMiddlewareResult(payload=request, original_payload=request,
                                   changed=False, trace=[])
original_request = _safe_copy(request)      # deepcopy only when someone is listening
current_request  = _safe_copy(original_request)
```

**(b) The genuinely hot loop — per-token streaming — is structurally off the plugin path.**
`on_stream_delta` fires once per token. Hermes does **not** call plugins there. Instead each
`(hook, callback)` pair gets its own bounded queue and its own daemon thread:

```python
# agent/plugin_stream_hooks.py:120-144
def enqueue_plugin_stream_hook(hook_name: str, **payload: Any) -> bool:
    """Queue an observer hook for each consumer without running plugin code inline."""
    item = dict(payload)
    for dispatcher in _dispatchers_for(hook_name):
        try:
            dispatcher.events.put_nowait(item); queued = True; continue
        except queue.Full:
            try: dispatcher.events.get_nowait(); dispatcher.events.task_done()   # drop OLDEST
            except queue.Empty: pass
        try: dispatcher.events.put_nowait(item); queued = True
        except queue.Full: logger.debug("plugin stream hook queue full after drop-oldest: ...")
```

`_QUEUE_SIZE = 1024` (`:16`); these hooks are documented as observers whose return values are
ignored (`hermes_cli/plugins.py:165-169`), so a slow plugin can never throttle the token stream — it
just loses events. This is the single most important pattern in the file for Aleph.

**(c) The tool catalog is memoized against a registry generation counter.** Assembling the model's
tool list means walking every registered tool, running availability `check_fn`s, and applying dynamic
schema overrides. It is recomputed only when something actually changed:

```python
# tools/registry.py:451-457
# Monotonically-increasing generation counter. Bumped on every mutation
# (register / deregister / register_toolset_alias / MCP refresh). External
# callers (e.g. get_tool_definitions) can memoize against it: a cache entry
# keyed on the generation is valid for as long as the generation hasn't changed.
self._generation: int = 0
```

```python
# model_tools.py:363-376 (the memo key)
cache_key = (
    registry.current_scope_key(),
    frozenset(enabled_toolsets) ..., frozenset(disabled_toolsets) ...,
    registry._generation,          # ← plugin load/unload invalidates automatically
    cfg_fp,                        # ← (config mtime_ns, size): user edits invalidate
    ..., profile_scope,
)
```

with a second TTL cache one level down on the availability probes:

```python
# tools/registry.py:1018-1028
"""``check_fn()`` results are cached for ~30 s via _check_fn_cached to amortize
repeat probes (check_terminal_requirements probes modal/docker, browser checks
probe playwright, etc.)..."""
```

**(d) Discovery cost is measured and overlapped.** Two named numbers:

```python
# hermes_cli/plugins.py:5684-5692
"""Discovery costs ~150ms of manifest scanning + module imports on the CLI
startup path. Interactive chat doesn't need plugins until the first agent
turn, so callers on that path can start discovery here and let it overlap
the CPU/subprocess-heavy rest of startup."""
```

```python
# tools/registry.py:111-121
"""The per-file AST scan (_module_registers_tools) costs ~145 ms over ~100 files
on a warm cache, so verdicts are memoized on disk keyed by (mtime_ns, size)."""
```

Callers that only need a conservative answer while discovery is in flight read **last launch's
persisted answer** rather than blocking (`get_plugin_toolset_keys_nowait`,
`hermes_cli/plugins.py:5763-5787` — "a stale set from the last launch is harmless and self-heals as
soon as discovery lands").

**(e) The strongest structural idea: register from the manifest, import on first use.** A bundled
chat-platform plugin is registered as a *deferred loader* — the manifest declares the surface, the
Python module (and its heavy SDK) is never imported until the gateway actually asks for that
platform:

```python
# hermes_cli/plugins.py:4437-4446
def _register_deferred_platform(self, manifest) -> None:
    """The platform adapter module is imported only when the gateway / cron /
    setup / send_message path first asks the platform_registry for this
    platform. Until then we record a lightweight LoadedPlugin so
    `hermes plugins list` still shows the platform as available, and we hand
    the registry a loader that runs the normal eager-load path."""
```

and it goes one level finer — a platform plugin's *outbound client tools* (light) are split from its
*inbound adapter* (heavy, imports `discord.py`), so the light half is live with zero import
(`_register_deferred_platform_tools`, `:4524`).

**(f) Out-of-process plugins (MCP) cache their schema on disk** so the child process is not spawned
just to learn what tools it offers:

```python
# tools/mcp_schema_cache.py:1-7
"""Persistent MCP tool-schema cache for lazy server startup.
Stores per-server tool manifests on disk so Hermes can register MCP tools into
the agent snapshot without spawning the stdio child process at idle dashboard
startup. Cache entries are keyed by server name + a fingerprint of the
connection config (command/args/url/tools filters)."""
```

**(g) The real currency is tokens, not microseconds.** Two design decisions make this explicit.
`execute_code` lets the model write a Python script that calls Hermes tools over a Unix socket, so a
multi-step pipeline costs **one** inference turn:

```python
# tools/code_execution_tool.py:1-26
"""Lets the LLM write a Python script that calls Hermes tools via RPC,
collapsing multi-step tool chains into a single inference turn.
...
In both cases, only the script's stdout is returned to the LLM; intermediate
tool results never enter the context window."""
```

And the self-improvement fork picks its payload based on **prompt-cache economics**:

```python
# agent/background_review.py:35-45
# The review fork runs on the MAIN model by default ("auto"), replaying the
# full conversation — already warm in the prompt cache, so cheap cache reads.
# ... A different model cannot reuse the parent's cache (different key), so the
# fork is cold regardless — replaying the full transcript would just cold-write
# it. So when (and only when) routed to a different model, we replay a compact
# DIGEST to minimise cold-written tokens. Same model -> full replay; different
# model -> digest. That's the whole policy.
```

Also: `pre_llm_call` context is documented as being injected into the **user message, never the
system prompt**, "so cached tokens are reused" (`hermes_cli/plugins.py:5092-5099`). Prompt-cache
stability is treated as part of the plugin API contract.

### 3.3 Where it still pays a tax it did not have to

- **`inspect.signature()` on every hook invocation.** `_invoke_hook_callback`
  (`hermes_cli/plugins.py:5044-5069`) calls `inspect.signature(callback)` per call to decide which
  additive kwargs an older narrow-signature callback can accept. No cache. For a hook fired per tool
  call this is tens of microseconds of pure reflection. **Fix for Aleph: compute the accepted-kwargs
  set once at registration and store it on the registration record.**
- **`get_entry` rebuilds a merged dict on every lookup.** `_merged_tools`
  (`tools/registry.py:462-467`) does `merged = dict(self._tools); merged.update(scoped)` inside the
  registry lock on *every* `get_entry` — i.e. on every tool dispatch. There is a generation counter
  right there that could key a merged-view cache; it isn't used for this.
- **The event bus deep-copies the payload per subscriber** (`hermes_cli/plugins.py:5240-5243`) —
  correct for isolation, expensive for large payloads, and there is no size cap.

### 3.4 The plugin-to-plugin event bus

Namespaced, host-owned, single worker thread, bounded, recursion-capped:

```python
# hermes_cli/plugins.py:3213-3264 (ctx.emit)
"""The event is delivered as ``<plugin_key>:<event>`` where plugin_key is
FORCED to this plugin's own registry key. Pass only the bare event name — a
plugin may only publish under its own namespace. ... Delivery is
fire-and-forget through a host-owned, single-worker queue: registration order
is preserved, while a blocking subscriber cannot stall the emitter."""
```

Constants: `HERMES_EVENT_NAMESPACE = "hermes"` reserved for core (`:527`),
`_EVENT_EMIT_DEPTH_CAP = 8` (`:533`), `_EVENT_PENDING_CAP = 64` (`:537`). Subscribing is
unrestricted; only *emitting* is namespace-gated. Each queued event carries a **generation** number
and the exact subscription objects it was queued against, so an unload between enqueue and dispatch
drops the callback rather than invoking a zombie (`_deliver_event`, `:5216-5252`).

---

## 4. Lifecycle

### The ownership ledger

Every registration made through `PluginContext` returns a handle carrying its own inverse:

```python
# hermes_cli/plugins.py:1168-1214
@dataclass
class PluginRegistration:
    """One host-owned registration made while loading a plugin.

    Plugins only receive the context registration APIs; the manager owns the
    matching cleanup operation.  Keeping that inverse operation beside the
    registration lets a force reload unwind global registries in reverse
    order, including an override that needs to restore the entry it replaced."""
    kind: str
    key: str
    release: Callable[[], None]
    plugin_key: str = ""
    def dispose(self) -> None:
        if self._disposed: return
        self._disposed = True
        try: self.release()
        finally:
            if self._on_dispose is not None: self._on_dispose(self)
```

Unload walks that ledger in **reverse acquisition order**, best-effort, one try/except per
registration (`_dispose_registrations`, `:3583-3600`; `_unload_scoped`, `:3618-3735`). Beyond tool
and hook registrations the same ledger covers `on_unload` callbacks and supervised background tasks:

```python
# hermes_cli/plugins.py:1637-1656
def spawn_task(self, coro, *, name=None) -> "asyncio.Task":
    """The task is recorded in the ownership ledger; unloading the plugin
    (or a force reload) cancels it."""
    task = loop.create_task(coro, name=task_name)
    def _cancel_task():
        if not task.done(): task.cancel()
    handle = self._track("background_task", task_name, _cancel_task)
    task.add_done_callback(lambda _t: handle.dispose())
```

### The replacement lease — the sharpest idea in the repo

Plain "undo my registration" is wrong when two plugins both override the same slot and unload out of
order. `registration_lifecycle.py` (128 lines, the whole file) models **generations**:

```python
# registration_lifecycle.py:1-6
"""Ownership leases for replaceable runtime registrations.

The coordinator models registration *generations*, not just value identity.
That distinction matters when the same provider singleton is registered again
after an older ownership generation was unloaded."""
```

```python
# registration_lifecycle.py:88-120  (ReplacementCoordinator.dispose)
latest = next((c for c in reversed(leases) if c.active), None)
lease.active = False
# "An older generation can share the exact same object identity as a newer one.
#  Registry-level CAS cannot distinguish those leases, so only the latest live
#  generation is allowed to mutate the slot."
if latest is lease:
    replacement = lease.previous
    predecessor = lease.predecessor
    while predecessor is not None:
        if predecessor.active:
            replacement = predecessor.current; break
        replacement = predecessor.previous
        predecessor = predecessor.predecessor
    lease.restore(replacement)
```

Read that again in Aleph terms: **the inverse of "I replaced X" is not "put X back", it is "restore
the nearest still-live predecessor generation."** Aleph's kernel has revertible effects; this is the
correct semantics for a *replaceable slot*, which Aleph's hot-replacement feature needs and which a
naive undo gets wrong.

### Load is transactional

A `register()` that raises halfway rolls back everything it registered, including event subscriptions
made before the throw:

```python
# hermes_cli/plugins.py:4847-4862
except Exception as exc:
    owned = [r for r in self._registration_order if r.plugin_key == plugin_key]
    self._dispose_registrations(owned)
    self._forget_registrations(owned)
    loaded.error = str(exc)
    # register() may have subscribed before raising. Remove those owner-tagged
    # entries so a failed/unloaded plugin cannot leave a callable reachable
    # from later event dispatch.
    self._remove_plugin_subscriptions(plugin_key)
```

### Reload

`discover_plugins(force=True)` → `unload()` all → rescan → reload, **in-process, no restart**. The
hard part they got right is `sys.modules` hygiene: relative imports inside a plugin
(`from . import foo`) are cached as `hermes_plugins.<slug>.foo` and would silently serve stale code,
so the package *and every submodule under it* is evicted before re-exec, and again if `exec_module`
raises (`_load_directory_module`, `:4988-5017`; `_clear_plugin_submodules`, `:5568-5599`).

### What is NOT handled

**In-flight work during a swap.** There is no drain, no quiesce, no deadline. A tool call already
executing inside a plugin handler simply keeps running against a module that is no longer registered.
The event bus alone tracks generations well enough to drop *queued* work (`:5153-5180`) but
explicitly cannot stop a running callback: *"A currently-running callback cannot be force-killed
safely."*

**There is no timeout on any hook or middleware callback.** `invoke_hook` (`:5071-5115`) is a bare
`for cb in callbacks: try: cb(...) except: log`. This is notable because their own research spike
identified deadline budgets as *the* differentiating gap in the field (§10) and then did not build
them.

---

## 5. Failure and blast radius

| Failure | Behaviour | Evidence |
|---|---|---|
| Plugin raises at import/`register()` | that plugin is skipped, everything it registered is rolled back, others load | `plugins.py:4847-4862` |
| Plugin hook raises at runtime | logged and swallowed; the loop continues | `plugins.py:5108-5114` |
| Plugin tool raises | converted to `{"error": ...}` and sanitized before the model sees it | `registry.py:1136-1146` |
| Plugin event subscriber raises | logged per subscriber; siblings still run | `plugins.py:5244-5251` |
| Plugin **hangs** | **hangs the agent** (no timeout anywhere) | absence, `plugins.py:5071` |
| Plugin returns a bad type | replaced by a contract error | `registry.py:1072-1100` |
| Bad plugin package installed lazily | can only *add* modules, never shadow core | `tools/lazy_deps.py:34-46` |
| Removing a plugin others depend on | **allowed, with a warning** | `plugins_cmd.py:1691`, `plugins.py:894-902` |

The last row is the gap Aleph has already closed and Hermes has not: **there is no protected-core
concept and no load-bearing refusal.** `cmd_disable` writes a name into a disabled list; nothing
checks whether another plugin declared `requires_plugins: [that one]`.

The lazy-install containment is worth quoting because it is the cheapest structural blast-radius
control in the repo:

```python
# tools/lazy_deps.py:34-46
# That directory is **appended to the end of ``sys.path``** — never prepended,
# never exported via ``PYTHONPATH`` — so the agent's own site-packages wins
# every name collision. A package installed this way can only ADD new importable
# modules; it can never shadow, downgrade, or break a module the core already
# ships. ... This is the structural guarantee that a lazily installed package
# cannot brick Hermes, which is what made it safe to seal the venv in the first place.
```

---

## 6. Trust and agent-authored code

### Honest about the boundary

```python
# hermes_cli/plugin_capabilities.py:6-12
**This is NOT a sandbox.** In-process Python plugins remain trusted code — a
malicious plugin can import anything, monkey-patch core, and ignore all of
this. Capabilities govern the *host API surfaces* Hermes hands out (which
registrations succeed, which ``ctx`` methods are live) and give the user an
honest consent + audit trail. Actual isolation is a separate research track.
```

Aleph should adopt this framing verbatim in its own docs. A capability system that is honest about
being a *consent and audit* mechanism rather than an isolation mechanism is far more useful than one
that implies a boundary it does not enforce.

### The capability model, and the one idea to steal from it

Seven capabilities, each mapped 1:1 to a gate that **already exists** on an enforcing surface
(`CAPABILITY_REGISTRY`, `hermes_cli/plugin_capabilities.py:78-131`): `tools.override`,
`llm.{provider,model,agent_id,profile,task}_override`, `gateway.platform_actions`. The design rule
is stated explicitly: *"We deliberately do not mint capability ids without an enforcing gate."*

The stealable idea is **consent hashing**:

```python
# hermes_cli/plugin_capabilities.py:181-184
def capability_set_hash(capabilities) -> str:
    """Deterministic sha256 over a capability set (order-insensitive)."""
    canon = "\n".join(sorted(set(capabilities)))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
```

The stored consent record is `{hash, granted_at}` — *what the user saw when they agreed*. When an
update declares a different capability set, the additions stay ungranted until re-consent
(`declared_set_changed`, `:379-391`; `pending_capabilities`, `:361-377`). Every check emits a
structured audit line with an `evidence` field naming *why* it was allowed
(`_log_capability_decision`, `:266-274`). Ground rule: any failure to read consent state means **not
granted** (`_plugin_entry`, `:196-210`, bare `except: return {}`).

### The install-time scanner, and its most useful lesson

`tools/plugin_guard.py` reuses the *skill* threat-pattern engine but tunes it, because a naive reuse
flags every legitimate plugin:

```python
# tools/plugin_guard.py:16-30
Plugins are strictly more dangerous than skills — they run Python in-process
with the agent — but they are also *expected* to do things a skill never
should: read their own API keys from environment variables ... So this scanner:
- Runs the full skills_guard pattern set on documentation/config files ...
- Exempts the "reads own env secret" / "HTTP call with key" pattern family on
  *code* files, while keeping genuinely malicious signals ...
```

Verdicts map to policy: `safe` → install; `caution` → confirm (`--force` overrides);
`dangerous` → blocked, **`--force` does not override** (`should_allow_plugin_install`, `:270-296`).

**Lesson for Aleph's AST gate:** a gate calibrated on the wrong artifact class flags everything and
gets disabled. Aleph's gate inspects *agent-written code*; the exemption set has to be derived from
what legitimate agent-written code actually does, or the gate will be turned off within a week.

### `hermes plugins doctor` — validate by really loading it, disposably

```python
# hermes_cli/plugin_dev.py:34-41
@contextmanager
def _doctor_runtime(plugin_path: Path):
    """Load one plugin through the real runtime and restore global state.

    This is deliberately private Doctor machinery, not a standalone plugin
    test framework. Registration code executes under a temporary HERMES_HOME
    with outbound socket connects blocked."""
```

Network is denied by patching (`_deny_network`, `:30-31`), a temp home is created, the plugin tree is
copied in, `register()` runs for real, and the ledger unwinds it. **This is the honest way to gate
agent-authored code: don't try to prove it safe statically, run it in a disposable scope and see what
it claims.** Aleph's kernel already has scoped capabilities and LIFO unwind — this pattern is nearly
free to build on top.

### Agent-authored capability is Markdown

`tools/skill_manager_tool.py:1-33` — the agent creates/edits/deletes skills under `~/.hermes/skills/`.
`agent/background_review.py:1-17` forks the agent after every turn, replays the conversation, and
asks *"should any skill/memory be saved or updated?"*, running under a **tool whitelist limited to
memory and skill management tools; everything else is denied at runtime**. `agent/learn_prompt.py`
builds the `/learn` prompt that turns anything the user points at into a skill, embedding the
house-style authoring rules directly in the prompt.

The agent can therefore *acquire abilities* without ever emitting Python. The boundary is: prose
enters the context window, code does not enter the process.

---

## 7. State and context — how a plugin reaches shared services

Via the `ctx` object (`PluginContext`, `hermes_cli/plugins.py:1388-3387`), which is a **facade**, not
a service locator handing out live internals.

- **Config** — `ctx.get_config(key)` / `ctx.set_config(key, value)` read and write only
  `plugins.entries.<plugin_id>.settings.<key>`. Keys are validated against a regex, path traversal
  and reserved roots (`model`, `plugins`, `security`, `settings`) are rejected before any config
  access (`_plugin_relative_segments`, `:1215-1241`; `_PLUGIN_SETTING_RESERVED_ROOTS`, `:1385`).
  Writes take a cross-process file lock plus an in-process lock so sibling plugins' read-modify-write
  transactions cannot drop each other (`:1489-1503`).
- **Durable state** — `ctx.state` is a profile-scoped JSON store with a **10 MB quota**
  (`_PLUGIN_STATE_QUOTA_BYTES`, `:1383`) and validated keys (`PluginState`, `:1315-1380`).
- **LLM** — `ctx.llm` returns a host-owned `PluginLlm` facade bound to the plugin id
  (`:1565-1579`); the plugin never sees credentials, and provider/model/profile overrides are
  fail-closed capabilities.
- **Subagents** — `ctx.subagent_lifecycle` (`:1581-1600`): *"Plugins receive serializable handles and
  immutable snapshots; they never receive a live agent or a private registry."*
- **Platform actions** — `ctx.platform_actions` (`:1514-1528`): a minimal verb set
  (`add_reaction`, `set_thread_title`) that re-checks the capability on **every call** and returns
  `{"ok": bool, ...}` — *"verbs never raise into hook dispatch. No adapter handles or raw SDK objects
  are exposed."*
- **MCP** — `ctx.call_mcp(server, tool, args)` (`:1811-1887`) routes through the existing MCP client
  (background loop, trust tiers, circuit breaker) and is **default-off**: a plugin reaches no server
  until the operator lists it in `plugins.entries.<id>.mcp_allowlist`.

Everything is **scoped by profile.** `PluginManager` is cached per resolved `HERMES_HOME`
(`get_plugin_manager`, `:5601-5633`), the tool registry keeps per-scope overlays
(`_scoped_tools`, `tools/registry.py:432-435`), and modules get a scope-hashed name when two profiles
claim the same slug (`_directory_module_name`, `:4931-4945`). This is a genuinely good multi-tenancy
story inside one process.

**The caveat:** none of this stops a plugin from `import`ing anything it likes and reaching straight
into `hermes_state` or `tools.registry`. The facade is a convenience and a consent surface, not a
boundary — which the docs say plainly.

---

## 8. Concurrency model

Mixed, and deliberately so.

- **Async** in the gateway (`gateway/run.py`, asyncio) and adapters.
- **Threads** everywhere else: tool execution fans onto a thread pool, plugin discovery runs in a
  daemon thread, the plugin event bus has one worker thread, each streaming-hook consumer has its own
  thread.
- **Processes** for subagents' terminals, `execute_code` sandboxes, MCP stdio servers, and kanban
  workers (each a separate `hermes … chat -q` subprocess).

Two pieces of thread machinery worth stealing:

```python
# tools/thread_context.py:1-31
"""A bare ``threading.Thread`` / ``ThreadPoolExecutor`` worker starts with an
empty ``contextvars.Context`` and no thread-local approval/sudo callbacks.
Tool dispatch inside such a thread therefore silently loses:
  * the approval *session/platform* ContextVars ... so gateway sessions fall
    into check_dangerous_command's non-interactive auto-approve branch and
    dangerous commands run without prompting (#33057, #30882);
  * the thread-local CLI approval/sudo callbacks ... (GHSA-qg5c-hvr5-hjgr)"""
```

That is a **security bug caused by losing context at a concurrency boundary**, fixed by one audited
`propagate_context_to_thread` helper. Aleph's coeffect/capability context has exactly this hazard.

```python
# tools/daemon_pool.py:1-25
"""Stdlib ThreadPoolExecutor workers are non-daemon AND are registered in
concurrent.futures.thread._threads_queues, whose atexit hook joins every worker
unconditionally — even after shutdown(wait=False). A single wedged worker
therefore blocks interpreter exit forever. ... Do NOT use it for work that must
complete before exit (durable writes)."""
```

**What prevents shared-state corruption:** a `threading.RLock` in the tool registry with snapshot
reads (`tools/registry.py:445-447`, `_snapshot_state`), a separate `_discovery_lock` +
`_event_lock` in the plugin manager, per-plugin file locks for state, a deep copy per event
subscriber, and `MappingProxyType` for the session info passed to prompt-section renderers
(`:5321-5330`). It is careful, but it is lock discipline, not a structural guarantee.

---

## 9. What an agent/tool actually is, and how tools reach models

**A tool** is a `ToolEntry` (`tools/registry.py:204-235`): name, toolset, JSON schema, handler,
optional `check_fn` availability probe, `requires_env`, `is_async`, emoji, `max_result_size_chars`,
and an optional `dynamic_schema_overrides` callable.

Built-in tools **self-register at import**. Discovery is an AST scan looking for a top-level
`registry.register(...)` call, so a helper module that calls it inside a function is not picked up:

```python
# tools/registry.py:74-86
def _is_registry_register_call(node) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call): return False
    func = node.value.func
    return (isinstance(func, ast.Attribute) and func.attr == "register"
            and isinstance(func.value, ast.Name) and func.value.id == "registry")
```

**Toolsets** (`toolsets.py`, 1,083 lines) group tools and compose from other toolsets; a session
enables toolsets, not individual tools.

**Exposure to the model** is `registry.get_definitions()` → OpenAI-format `{"type":"function", ...}`
(`tools/registry.py:1018-1065`), filtered by `check_fn`, with per-call dynamic schema rewriting so
the model sees the user's *actual* limits rather than framework defaults:

```python
# tools/delegate_tool.py:4731-4738
# NOTE: description / tasks.description / role.description are placeholder
# values. The real text is generated per get_definitions() call by
# _build_dynamic_schema_overrides() ... so the model sees the user's actual
# delegation.max_concurrent_children / max_spawn_depth, not the framework defaults.
```

**An agent** is an `AIAgent` (`run_agent.py`, 9,053 lines) with a conversation, a toolset, a model
binding, a terminal session, and a `task_id`. Multi-agent structure:

- **Delegation** (`tools/delegate_tool.py`, 4,926 lines) spawns child `AIAgent`s with **a fresh
  conversation (no parent history)**, their own `task_id`/terminal, and the parent's toolsets minus
  `DELEGATE_BLOCKED_TOOLS` = `{delegate_task, clarify, memory, send_message, cronjob}` (`:52-60`) —
  a leaf child cannot recurse, talk to the user, write shared memory, message platforms, or schedule
  work in the parent's name.
- **Roles**: `leaf` (default, cannot delegate) vs `orchestrator` (can, bounded by
  `delegation.max_spawn_depth`, with a global kill switch `delegation.orchestrator_enabled`,
  `:1012-1023`). Depth is enforced, not advisory (`:1622-1623`).
- **Result sharing is by summary, not shared state**: *"The parent's context only sees the delegation
  call and the summary result, never the child's intermediate tool calls or reasoning."*
  (`tools/delegate_tool.py:14-17`). Optionally the parent enforces a JSON Schema on the child's final
  answer with **one bounded correction retry** (`DELEGATE_TASK_SCHEMA`, `:4776-4790`).
- **Routing** is the model's choice via a tool call, plus a separate deterministic router: the
  **kanban** board (`hermes_cli/kanban_db.py`) dispatches queued tasks to worker subprocesses with
  claims, TTLs, heartbeats, crash detection and stale-claim reclaim.
- Parallel batch delegation runs on a `DaemonThreadPoolExecutor(max_workers=max_children)`
  (`:3921-3922`).

---

## 10. Memory and knowledge across runs

Four layers, and Aleph should read this as *what not to settle for*:

1. **Transcripts in SQLite with FTS5.** `hermes_state_common.py:249-430` defines 11 tables
   (`sessions`, `messages`, `system_prompts`, `session_model_usage`, `async_delegations`, …) plus
   `messages_fts` and `messages_fts_trigram` virtual tables. Schema version is past v23 with real
   migrations (`hermes_state_schema.py`), an FTS5-availability probe, and a trigram table for
   CJK/substring search. `tools/session_search_tool.py` lets the agent search its own past
   conversations, with LLM summarization on top.
2. **Curated prose** — `MEMORY.md` and `USER.md`, written by the agent through the `memory` tool,
   with periodic "nudges" (`agent/curator.py`, `agent/memory_manager.py`).
3. **Skills** — procedural memory as Markdown (`skills/`, `~/.hermes/skills/`), indexed into the
   system prompt by a ≤60-character description and loaded on demand via `skill_view` (progressive
   disclosure). `agent/learning_graph.py` renders skills + memory chunks as a graph, with
   skill↔skill edges from declared `related_skills` and memory↔skill edges from **lexical overlap**.
4. **Pluggable external memory** — an `exclusive` plugin category (exactly one active) selected by
   `memory.provider`. The ABC is a full per-turn lifecycle (`agent/memory_provider.py:14-33`):
   `initialize / system_prompt_block / prefetch(query) / sync_turn / get_tool_schemas /
   handle_tool_call / shutdown`, plus optional `on_pre_compress`, `on_session_end`, `on_delegation`.
   Eight providers ship (Honcho, Hindsight, Mem0, OpenViking, RetainDB, holographic, byterover,
   supermemory).

Two details worth taking:

- **Bundled-wins precedence, deliberately inverted from the general plugin system**, with a stated
  reason: *"A memory provider is activated by name, so letting a directory dropped into the working
  tree shadow a shipped provider would silently redirect the agent's memory. Changing this order is a
  breaking change, not a cleanup."* (`plugins/memory/__init__.py:16-21`)
- **A prefetch gate that skips the network round-trip on turns with no semantic signal** —
  `TRIVIAL_PROMPT_RE` (`agent/memory_provider.py:69-84`) matches "ok", "thanks", "go ahead", slash
  commands, and is shared by core and providers *"so the two can never drift apart."*

**The honest comparison to Aleph:** there is nothing here resembling an evidence-anchored claim
layer. Knowledge is (a) raw transcript text, (b) LLM-written prose, (c) LLM-written procedure. There
are no claims, no citations, no verbatim anchors, no contradiction handling, no retraction blast
radius. Lexical overlap is the strongest relation the graph has. Aleph's belief-engine ambition is
*strictly beyond* what the largest agent harness in this survey attempts — Hermes offers Aleph
nothing to copy on that axis, only a warning about what "memory" degenerates into when nobody makes
claims first-class.

---

## 11. The distinctive bet, and what is genuinely novel here

**The bet:** *an agent improves by writing its own documentation, not its own code, and the harness's
job is to make that loop cheap, safe and always-on.* Everything distinctive follows from it — the
`/learn` command, autonomous skill creation, the post-turn background review fork, the memory nudges,
FTS5 session search, `agentskills.io` compatibility. The plugin system exists to let *humans* extend
the host; the learning loop exists to let the *agent* extend itself, and the two never meet.

**Genuinely novel versus the other four codebases in this survey:**

1. **`registration_lifecycle.py` — replacement leases with predecessor restoration.** I have not seen
   this modelled anywhere else. It is 128 lines and it is the correct semantics for hot replacement.
2. **`docs/rfcs/2026-07-plugin-architecture-lessons-pi-opencode.md`** — a 134-line source-level
   comparative spike of two other harnesses (Pi @ `eb79351`, OpenCode @ `c69abee`), with
   `file:line` citations into pinned commits, a 13-row **Adopt/Adapt/Avoid** table, and a "verified
   absences" section distinguishing *"we couldn't find it"* from *"it isn't there."* This is a better
   artifact than most of the code. It is also the exact exercise the Aleph owner is running.
3. **Consent hashing on the declared capability set** (§6) — capability drift on update is a real
   problem nobody else in this space handles.
4. **`execute_code`'s programmatic tool calling** — the model writes a script; tools are called over
   a Unix socket back into the parent; **only stdout enters the context**. It treats the context
   window as the scarce resource and collapses a pipeline into one turn.
5. **Prompt-cache stability as a plugin API contract** — hook-injected context goes in the user
   message, never the system prompt; the review fork chooses full-replay vs digest by cache-key
   identity.
6. **Deferred platform loading with a light/heavy split** — manifest registers the surface, module
   import waits for first use, and the plugin's cheap client tools are separated from its expensive
   adapter.

---

## 12. The single best idea, and the single worst

**Best: the ownership ledger + replacement lease** (`hermes_cli/plugins.py:1168-1214` +
`registration_lifecycle.py`). Not merely "record an undo" — Aleph already has that — but the specific
insight that *a replaceable slot needs generational ownership*, so that unloading out of order
restores the nearest still-live predecessor rather than an object that may itself have been
superseded or removed. Combined with transactional load rollback (`:4847-4862`) and reverse-order
disposal (`:3583-3600`), this is the most complete lifecycle story in the survey.

**Worst: 23 hard-coded `register_*` methods on `PluginContext`, each a near-verbatim copy.** Adding a
capability *category* to Hermes means patching the core in four places (a registry module, a
`register_X_provider` method, a tracked-registration kind, a `plugins list` display case). The bodies
of `register_tts_provider` (`:2648`), `register_video_gen_provider` (`:2428`),
`register_web_search_provider` (`:2477`), `register_browser_provider` (`:2527`),
`register_image_gen_provider` (`:2318`), `register_transcription_provider` (`:2708`) and
`register_dashboard_auth_provider` (`:2367`) are the *same five steps* with different imports. The
abstraction that wanted to exist — one `register_provider(kind, provider)` over a
registry-of-registries, with `_track_replacement` doing the generational work it already does — was
never extracted. That single omission is what converts "everything is a plugin" into "everything is a
plugin *of a kind the host already knows about*."

Runner-up worst: **20,767-line `cli.py` and 13,086-line `hermes_state.py`.** Whatever the process
that produced this repo, it optimizes for appending to existing files.

---

## Worth stealing for Aleph

1. **Replacement leases for any *shared slot* multiple plugins can claim.**
   `registration_lifecycle.py:43-120`. Aleph's `Kernel.replace`
   (`packages/aleph-kernel/src/aleph_kernel/kernel.py:240-286`) is already correct for a *named,
   single-owner* capability: it tears down, re-activates, and restores `previous` on failure. The
   lease matters for the case Aleph does not have yet and will the moment plugins register into any
   shared registry — a tool name, a connector kind, a renderer — where **two plugins claim one slot
   and unload out of order**. There the inverse of a replacement is not *"restore the value I saw"*
   but *"restore the nearest still-live predecessor generation"*, and only the latest live generation
   may mutate the slot at all. Build that once, in the kernel, before the first shared registry
   appears.

2. **Manifest-eager, module-lazy loading — with a light/heavy split inside one plugin.**
   `hermes_cli/plugins.py:4437-4523`. This is the direct answer to *"how do I not get crazy slow."*
   A boot manifest that declares what a plugin provides means the kernel can wire the graph, show the
   capability, and route to it **without importing it**. Aleph should make this the default for every
   plugin, not a special case for platforms, and should let a manifest declare which surfaces are
   cheap (register now) versus expensive (register a loader).

3. **A generation counter on every registry, and memoize the composed view against it.**
   `tools/registry.py:451-457` + `model_tools.py:363-376`. Aleph's kernel already computes a
   dependency graph; give it a monotonic generation, bump it on every mount/unmount, and key every
   derived artifact (tool catalog, capability resolution, prompt assembly) on
   `(generation, config-fingerprint)`. This is what makes plugin composition cost the same as a
   compiled system on the steady-state path: the composition is computed once per change, not once
   per call.

4. **Gate every fire site on `has_hook()`, and make observer hooks structurally non-blocking.**
   `hermes_cli/plugins.py:285-288` (the cost rule) and `agent/plugin_stream_hooks.py:120-144` (one
   bounded queue + one thread per consumer, drop-oldest, return values ignored). Aleph's rule should
   be: **a hot loop may only fire observer effects, and observers are queued, never awaited.** If a
   plugin needs to *change* something, it belongs on a cold path.

5. **Consent hashing + an evidence-bearing audit line on every capability check.**
   `hermes_cli/plugin_capabilities.py:181-184`, `:233-274`. An agent-authored plugin that quietly
   widens its declared capabilities on the next revision is the exact failure Aleph's spawn ledger
   and probation exist to catch; hashing the declared set and forcing re-consent on drift makes it
   mechanical. And log *why* a grant was allowed, not just that it was.

6. **Validate agent-written code by loading it for real in a disposable scope.**
   `hermes_cli/plugin_dev.py:34-60`. Temporary home, network denied, real `register()`, ledger
   unwind. Aleph's AST gate should be the *first* filter, not the only one — the second should be
   "mount it in a throwaway kernel scope and see which capabilities it actually claims," which the
   ledger makes cheap and reversible.

7. **Add-only extension paths.** `tools/lazy_deps.py:34-46` — appended to `sys.path`, never
   prepended, so an installed extension can add names but never shadow core ones. The generalization
   for Aleph: **an agent-authored plugin should be structurally incapable of shadowing a
   protected-core capability**, rather than being *checked* for it.

8. **Deadline budgets on plugin callbacks — the gap Hermes itself identified and did not fill.**
   `docs/rfcs/2026-07-plugin-architecture-lessons-pi-opencode.md` lesson 4: *"Neither system times
   out runtime hooks; both shipped hang-class failures... be the first framework to have them."*
   Then `invoke_hook` (`hermes_cli/plugins.py:5071-5115`) has no timeout. Observer callbacks:
   budget + log-and-drop. Guard/mutating callbacks: budget + fail closed. This is available to Aleph
   as a differentiator, pre-validated by three codebases' pain and nobody's implementation.

9. **Two honest framings to copy verbatim.** *"This is NOT a sandbox"*
   (`plugin_capabilities.py:6`) — say what the capability system is (consent + audit + which host
   surfaces answer) and what it is not. And *"we deliberately do not mint capability ids without an
   enforcing gate"* (`:33-36`) — every capability in Aleph's registry should name the code that
   refuses when it is absent, or it should not exist.

10. **Precompute callback signatures at registration.** Hermes reflects with
    `inspect.signature()` on every hook call (`:5044-5069`) to support additive payloads against
    narrow older callbacks. The idea (payloads evolve additively; old callbacks get only what they
    declare) is good and worth keeping; do the reflection **once, at registration**, and store the
    accepted-kwargs frozenset on the registration record.

11. **Propagate the capability/coeffect context across every concurrency boundary, through one
    audited helper.** `tools/thread_context.py:1-31` documents two CVEs-in-spirit caused by a
    `ContextVar` silently emptying in a worker thread — approvals fell through to auto-approve.
    Aleph's scoped capability access has the identical hazard the moment work fans out.

---

## Worth avoiding

1. **Do not let extension points be host-owned verbs.** 37 hard-coded hooks and 23 `register_*`
   methods mean every new capability *kind* is a core patch, and the near-identical bodies prove the
   duplication was never paid down. Aleph's stated goal — plugins that depend on plugins — dies here.
   Define the *replaceable-slot* abstraction once (it already exists, as `_track_replacement`) and
   make capability categories **data in a manifest**, not methods on a context class.

2. **Do not ship a dependency mechanism with no user.** `requires_plugins` orders loads and warns on
   absence, `ctx.has_plugin` probes at runtime — and **zero shipped plugins declare a dependency.**
   An unexercised contract is not a contract. Aleph's own rule ("ship a consumer with every
   producer") is exactly right and Hermes is a live demonstration of the failure.

3. **Do not confuse "load order respects dependencies" with "the graph is load-bearing."** There is
   no protected core, and `hermes plugins disable A` succeeds even if B declared `requires_plugins:
   [A]` (`hermes_cli/plugins_cmd.py:1691`). Aleph already has the better answer —
   `ProtectedCapability` and `DependentsWouldBreak` in
   `packages/aleph-kernel/src/aleph_kernel/kernel.py:260-336` — so this is a place to *keep* an
   existing advantage rather than import one, and to make sure it covers *deactivate/disable*, not
   only *unmount*.

4. **Do not leave hook dispatch unbounded in time.** No timeouts anywhere. A synchronous plugin
   callback that blocks on network I/O blocks the agent turn. Their own research says this is the
   field-wide gap; do not reproduce it.

5. **Do not ignore in-flight work during a swap.** Hermes cancels *queued* events by generation
   (`:5153-5180`) but explicitly cannot stop a running callback. Aleph's revertible effects need a
   quiesce/drain phase with a bounded deadline, or hot replacement will corrupt work in progress the
   first time it matters.

6. **Do not let files grow to 20,000 lines.** `cli.py` (20,767), `hermes_state.py` (13,086),
   `run_agent.py` (9,053), `tools/mcp_tool.py` (8,182), `hermes_cli/plugins.py` (6,561). The plugin
   system is the *only* subsystem here that is legible, and it is legible because someone extracted
   `registration_lifecycle.py`, `plugin_capabilities.py`, `middleware.py` and `plugin_stream_hooks.py`
   out of it.

7. **Do not build an agent memory layer out of prose and full-text search and call it knowledge.**
   Hermes's durable knowledge is transcripts + FTS5 + LLM-written Markdown + lexical-overlap edges.
   It works, it is cheap, and it cannot answer "what is the evidence for this, and what happens to it
   if that source is retracted." Aleph's claim spine is the harder and correct bet; nothing in the
   largest harness surveyed suggests otherwise.

---

## Licensing note

MIT, `Copyright (c) 2025 Nous Research`. Reuse would require preserving the copyright notice and
license text. **Nothing here should be vendored into Aleph.** The ideas worth taking are small and
structural — a lease coordinator, a generation counter, a queue-per-observer, a consent hash — and
are better reimplemented against Aleph's own kernel types than ported. If any code *is* ported (the
most tempting candidate is the ~128-line `registration_lifecycle.py`), it must carry a `NOTICE`
recording upstream, license and per-file lineage, per Aleph's standing rule. Note also that several
bundled plugins wrap third-party SDKs and services under their own licenses; `plugins/` is not
uniformly MIT-clean for redistribution purposes even though the repo is.
