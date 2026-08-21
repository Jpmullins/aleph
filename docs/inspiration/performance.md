# Will a plugin architecture make Aleph slow?

**The short answer: no, and the numbers are not close.**

On this machine, resolving a capability through Aleph's existing kernel and calling a method on it
costs **117 nanoseconds**. The cheapest useful thing Aleph does with the result — a `SELECT 1` to
Postgres — costs **38,550 nanoseconds**. The thing Aleph actually spends its life doing — one LLM
call — costs somewhere between **500,000,000 and 30,000,000,000 nanoseconds**.

A plugin dispatch is between **330×** and **250,000,000×** cheaper than the work it dispatches to.
You cannot make this system slow by adding plugin boundaries. You can only make it slow by putting a
plugin boundary somewhere it gets crossed millions of times, or by moving bulk data across one.

This document establishes that with measurements, names the exact places where the rule flips, and
turns the result into rules a reviewer can check.

---

## 0. How these numbers were produced

Everything in the tables below was **measured on this machine on 2026-08-19**, not quoted from
memory. Rows marked *(cited)* or *(inferred)* are explicitly labelled and are the only ones that
were not.

- **Hardware:** Apple M5 Max, 18 cores, macOS (Darwin 25.5.0).
- **Python:** CPython 3.13.14, the project venv at `/Users/jpmullins/Documents/code/aleph/.venv`, so
  `aleph_kernel`, `aleph_core`, `pydantic` 2.13.4, `orjson` 3.11.9, `asyncpg` 0.31.0, `mcp` 1.28.0
  and `numpy` 2.4.6 are the exact versions Aleph runs.
- **Postgres:** 17.10 (Homebrew), a throwaway cluster started on port 55432 with `fsync=off`,
  `synchronous_commit=off`, `shared_buffers=512MB`, loopback TCP only. That configuration makes
  Postgres look *faster* than production, which is the conservative direction for this argument.
- **Method:** `timeit` / `perf_counter_ns`, minimum of 5–7 repeats of an auto-ranged inner loop.
  Minimum, not mean, because we want the cost of the operation, not the cost of the OS scheduler.
- **Scripts:** the five benchmark files are listed in Appendix A with what each one does, so any
  number here can be re-derived or disputed.

**Two caveats you must carry into the reading.**

1. **macOS inflates one specific row.** An `asyncio` event-loop tick costs one selector syscall, and
   on this box `kqueue()` with a zero timeout measures **12.3 µs** — absurdly expensive. Isolating
   it: `KqueueSelector.select(0)` = 12,310 ns, `PollSelector` = 5,404 ns, `SelectSelector` = 486 ns
   for the same no-op. It is a Darwin syscall cost, not a Python cost. On Linux with `epoll` — where
   Aleph actually deploys — expect roughly 0.5–2 µs *(inferred, not measured here)*. Every
   loop-tick-derived number below (`await asyncio.sleep(0)`, `create_task` + await, the MCP figures)
   is therefore a **pessimistic upper bound** on Linux. The *design conclusions* do not change,
   because they all rest on the tick being 100–1000× a function call, which is true on both.
2. **Absolute numbers are machine-specific; ratios are the point.** An M5 Max is fast. A production
   container is slower. Every recommendation here is stated as a ratio or an order of magnitude, so
   it survives the move.

---

## 1. The cost budget

### 1.1 The floor: in-process Python

| operation | ns/op | vs. a function call |
|---|---:|---:|
| loop overhead (`pass`) | 2.2 | — |
| attribute read `obj.v` | 3.8 | 0.4× |
| dict lookup `d['db']` | 5.8 | 0.6× |
| **plain function call `f(1)`** | **10.2** | **1×** |
| bound method `obj.query(1)` | 10.8 | 1.1× |
| dict lookup + method call `d['db'].query(1)` | 12.8 | 1.3× |
| subclass method call | 11.1 | 1.1× |
| `functools.partial` call | 20.7 | 2.0× |
| `try/except` wrapper (no raise) | 18.5 | 1.8× |
| 3-deep middleware chain | 37.5 | 3.7× |
| 8-deep middleware chain | 83.0 | 8.1× |

**Read this as: a wrapper layer costs about one function call.** A pre/around/post pipeline three
deep costs 37 ns. Eight deep costs 83 ns. If you are worried that a plugin pipeline will be slow,
the pipeline is not what will make it slow.

### 1.2 Aleph's own kernel, measured against the real code

`packages/aleph-kernel/src/aleph_kernel/context.py:89-114` (`__getattr__` at :89, `get` at :101, `Store.get` at :55) — `Context.__getattr__` → `Context.get` →
`Store.get`.

| operation | ns/op | notes |
|---|---:|---|
| `Store.get(realm, key)` | 47.3 | tuple-key dict, root-realm fallback |
| `ctx.get("db")` | 75.9 | + `frozenset` membership check for the declaration gate |
| `ctx.db` (`__getattr__`) | 106.4 | + the `_`-prefix guard and the miss-path machinery |
| **`ctx.db.query(1)`** | **117.4** | the full cross-plugin call |
| `ctx.db` on an isolated (per-project realm) context | 129.6 | realm miss → root fallback |
| **hoisted `h = ctx.db` once, then `h.query(1)`** | **10.8** | **identical to a bare method call** |

Two facts matter enormously and Aleph should protect both.

**First, resolution is cheap in absolute terms.** 117 ns. For calibration, the cordis reviewer
modelled the equivalent path in cordis at **~1,427 ns** — 12× more — because `reflect.ts:71`
allocates a stack-capturing `new Error()` on *every* service access before it knows the access will
fail (~822 ns on its own), and every read mints two fresh Proxies.

**Second — and this is the part that is architecturally decisive — Aleph's overhead hoists to
literally zero.** `Context.get` returns the raw object. Assign it to a local once and the plugin
boundary *disappears from the machine code*: 10.8 ns, the same as calling a method on an object you
constructed yourself. cordis cannot do this; hoisting there still leaves ~190 ns/call because each
method read returns a new Proxy. **Aleph already has the property the owner is asking for.** Do not
trade it away for prettier diagnostics.

### 1.3 Event fan-out and reflection

| operation | ns/op |
|---|---:|
| fan-out to 0 listeners, naive `for h in hooks` | 19.5 |
| fan-out to 0 listeners, `if not hooks: return` first | 13.1 |
| fan-out to 3 listeners | 52.3 |
| build a 6-field dict (a per-token event payload) | 58.4 |
| **`inspect.signature(callback)` per call** | **2,830.9** |

The `inspect.signature` row is hermes-agent's live bug: `hermes_cli/plugins.py:5044-5069` calls it on
*every* hook invocation to decide which kwargs an older callback accepts. That is 2.8 µs — **24× the
cost of a full Aleph capability resolution** — spent on reflection that could have been computed once
at registration. Aleph must never introduce a per-call `inspect`, `typing.get_type_hints`, or
`dataclasses.fields` call.

Note the 58 ns dict-build row against prime-agent's admitted defect: `ExtensionRunner.emit()`
(`runner.ts:674-676`) allocates a fresh 14-getter context object *before* checking whether any
handler exists, on an event that fires once per streamed token. At ~2,000 tokens per response that
is real garbage for zero benefit. The fix is one line and Aleph should adopt it as a rule
(§8, rule 4).

### 1.4 Serialization — the expensive layer

Payload used throughout: an 8-hit `search_corpus` result — 8 chunks × ~1.2 KB of text plus ids,
scores and section paths. **10,732 bytes** as JSON. This is the realistic unit of Aleph retrieval.

| operation | ns/op | µs |
|---|---:|---:|
| `pydantic` validate 1 hit (already-parsed dict) | 425.6 | 0.43 |
| `pydantic` validate 8-hit result (dicts) | 2,561 | 2.6 |
| `pydantic` `model_validate_json` 8-hit (~10.7 KB) | 4,421 | 4.4 |
| `orjson.dumps` 8-hit result | 1,159 | 1.2 |
| `orjson` round trip 8-hit result | 4,320 | 4.3 |
| `json.dumps` 1 chunk (~1.1 KB) | 1,830 | 1.8 |
| `json.dumps` 8-hit result | 12,256 | 12.3 |
| `json.loads` 8-hit result | 6,669 | 6.7 |
| **`json` round trip 8-hit result** | **19,046** | **19.0** |
| `copy.deepcopy` 8-hit result | 9,553 | 9.6 |
| `json.dumps` 1,000 chunks (~1.1 MB) | 1,140,763 | 1,141 |
| `json.loads` 1,000 chunks (~1.1 MB) | 588,949 | 589 |
| `copy.deepcopy` 1,000 chunks | 531,612 | 532 |

**A stdlib-`json` round trip of one retrieval result costs 19 µs — 162 times an in-process capability
resolution.** Serializing 1,000 chunks costs 1.14 milliseconds, which is 9,700 capability
resolutions. Serialization, not dispatch, is the plugin tax. `orjson` is 10× better than `json` on
the same payload and Aleph already depends on it (`apps/api/pyproject.toml:12`).

`copy.deepcopy` deserves a specific warning: hermes-agent's plugin event bus deep-copies the payload
*per subscriber* (`hermes_cli/plugins.py:5240-5243`) with no size cap. On a 1,000-chunk payload with
three subscribers that is 1.6 ms of pure defensive copying. Aleph should pass immutable values
(frozen dataclasses, tuples) and skip the copy entirely.

### 1.5 Embeddings — the sharpest illustration in this document

| operation | value |
|---|---:|
| one 1024-dim embedding, as JSON text | **21,145 bytes** |
| `json.dumps` one embedding | **193.1 µs** |
| `json.loads` one embedding | 99.0 µs |
| `orjson.dumps` one embedding (numpy, `OPT_SERIALIZE_NUMPY`) | 9.6 µs |
| **`v.tobytes()` one embedding (4,096 raw bytes)** | **0.074 µs** |
| a batch of 64 embeddings as JSON | **1.35 MB** |
| `json.dumps` 64 embeddings | **12,385 µs (12.4 ms)** |
| `json.loads` 64 embeddings | 6,330 µs |
| **`B.tobytes()` 64 embeddings (262 KB raw)** | **2.87 µs** |

**Moving one embedding across a JSON boundary costs 2,600× more than moving it as bytes, and inflates
it 5× on the wire.** One batch of 64 costs 12.4 ms to serialize as JSON versus 2.9 µs as a buffer —
a factor of 4,300.

This single fact settles the design question. If a plugin boundary ever sits between "compute
embeddings" and "store embeddings" and the boundary is JSON, the plugin architecture will be
measurably, humiliatingly slow. If the boundary passes a handle — a chunk id, a source id, a
`memoryview` — it is free.

### 1.6 Async

| operation | ns/op | on Linux/epoll *(inferred)* |
|---|---:|---|
| `await` a coroutine that never suspends | 33.0 | same |
| `async with asyncio.Lock()` (uncontended) | 159.8 | same |
| `asyncio.Queue` `put_nowait` + `await get` (ready) | 185.0 | same |
| `loop.call_soon` | 225.7 | same |
| callback executed inside a *batched* loop tick | ~99 | same |
| **`await` a bare yield — one loop tick** | **13,111** | ~500–2,000 |
| `await asyncio.sleep(0)` | 13,429 | ~500–2,000 |
| **`ensure_future` + `await` a single task** | **27,127** | ~1,000–4,000 |
| 100 tasks created then `gather`ed (per task) | 1,550 | lower |
| `asyncio.to_thread` round trip | 27,613 | ~10,000–30,000 |

The decisive comparison: draining 50,000 callbacks inside **one** `_run_once` costs **99 ns each**;
giving each callback **its own** tick costs **13,320 ns each**. That is a **134× penalty for
scheduling granularity**, and it holds in reduced form on Linux.

**Design consequence:** never create one asyncio task per chunk, per token, or per row. Create one
task per batch, per request, per stream. A plugin API that hands out `await`-able per-item hooks is
an architectural mistake even though the `await` itself is only 33 ns — because the moment one
implementation of that hook actually suspends, you have bought a loop tick per item.

### 1.7 Crossing a process boundary

| operation | µs | vs. `ctx.db.query()` |
|---|---:|---:|
| `ctx.db.query(1)` in-process | 0.117 | 1× |
| unix socketpair round trip, 1 B | 6.15 | 52× |
| unix socketpair round trip, 150 B request | 6.51 | 55× |
| unix socketpair round trip, 10.7 KB result | 19.44 | 166× |
| TCP loopback round trip, 1 B | 15.35 | 131× |
| TCP loopback round trip, 10.7 KB | 18.32 | 156× |
| subprocess stdio JSON-RPC round trip, 150 B | 10.91 | 93× |
| subprocess stdio JSON-RPC round trip, 10.7 KB | 23.12 | 197× |
| HTTP/1.1 keep-alive POST loopback, 10 KB reply | 55.49 | 474× |
| HTTP POST, **new connection per call** | 158.78 | 1,357× |
| attach an 8 MB shared-memory segment + read | 8.00 | 68× |
| read 8 MB from an already-attached segment (`sum`) | 106.03 | — (13 GB/s) |
| **spawn a bare Python interpreter** | **11,323** | 96,800× |
| **spawn Python + `import json,asyncio,dataclasses`** | **28,143** | 240,500× |

Two things to take from this. **A local process boundary costs 6–160 µs**, i.e. 50–1,400 in-process
calls. That is affordable a few hundred times a second and ruinous a few hundred thousand times a
second. **Process *creation* costs 11–28 ms**, which is 100,000× an in-process call — so isolation
has to be paid once and amortized, never per call. (Aleph's `code-runner` spawns a child subprocess
per execution — `apps/code-runner/src/aleph_code_runner/executor.py:202` — which is exactly right for
agent-written code called seconds apart, and would be exactly wrong for anything called per chunk.)

### 1.8 Postgres

Real Postgres 17.10, 20,000 chunks of ~1 KB, GIN index on `text_tsv`, loopback TCP, `asyncpg`.

| operation | µs |
|---|---:|
| `SELECT 1` — pure round trip, prepared | 38.55 |
| `SELECT 1` — simple query protocol | 36.18 |
| PK lookup, 1 row with ~1 KB of text | 45.27 |
| `hydrate 40 chunks by id = ANY($1)` (~45 KB) | 90–116 |
| `BEGIN` + `SELECT 1` + `COMMIT` | 113.32 |
| 2 INSERTs in one transaction (row + ledger event) | 164.52 |
| pool `acquire` + `SELECT 1` + release | 130.02 |
| **`connect` + `SELECT 1` + `close` (unpooled)** | **1,592** |
| FTS + `ts_rank` + `LIMIT 40`, query matches **1.1%** of corpus | **483.84** |
| FTS + `ts_rank` + `LIMIT 40`, query matches **98.6%** of corpus | **29,531** |
| FTS + `ts_rank` + `LIMIT 40`, query matches **100%** of corpus | **30,574** |
| brute-force cosine over 20,000 × 1024 float32, top-40 (numpy) | 552 |
| brute-force cosine over 200,000 × 1024 float32, top-40 (numpy) | 5,718 |

**The cheapest possible Postgres interaction is 36 µs — 308 Aleph capability resolutions.** A
transaction with a ledger write is 165 µs — 1,400 resolutions. You could route every single database
call in Aleph through fourteen layers of plugin middleware and the added cost would be under 0.7% of
the query.

**And now the finding that actually matters for Aleph's speed, which has nothing to do with
plugins.** `aleph_rks.retrieval._hybrid_search` builds its lexical leg with `or_tsquery` — every
query term OR'd (`packages/aleph-rks/src/aleph_rks/retrieval.py:103-113`, and the module docstring
explains why: `plainto_tsquery` ANDs, so a natural-language question matched nothing). The
consequence is that **selectivity is set by the most common term in the question.** With rare terms
the query is 484 µs. Add one common word and 98.6% of the corpus becomes a candidate, `ts_rank` is
computed over all of it, and the same query costs **29.5 ms — 61× slower**, growing linearly with
corpus size. At 200,000 chunks that is ~300 ms per retrieval, per query, before the LLM sees
anything.

That is **250,000× the cost of the plugin dispatch that invoked it.** Aleph's retrieval performance
risk is a `tsquery` selectivity problem, not a plugin problem. Options: a `ts_rank` cutoff or
candidate `LIMIT` before ranking, dropping terms above a document-frequency threshold, or
`websearch_to_tsquery` with quoted phrase groups. It is out of scope for this document, but it
belongs in the retrieval owner's queue with a higher priority than anything about plugin overhead.

### 1.9 Instrumentation

| operation | ns/op |
|---|---:|
| `uuid.uuid4()` | 1,400 |
| `aleph_core.ids.uuid7()` | 1,394 |
| OTEL `start_as_current_span` (no exporter configured) | 1,480 |
| **`structlog` `.info()` with output discarded** | **6,309** |
| stdlib `logging.info()` | 2,128 |
| stdlib `logging.debug()` when DEBUG is filtered out | 47 |

**One `structlog` line costs 54 capability resolutions.** If you are ever tempted to argue about
plugin dispatch cost in a code review, check first whether the same function logs — the log line is
54× more expensive than the dispatch. Correspondingly: a level check that filters early costs 47 ns,
so guarded debug logging is genuinely free and unguarded structured logging in a per-item loop is
not.

### 1.10 The remote calls that dominate everything

| operation | measured / cited |
|---|---:|
| DNS lookup (warm resolver) | 0.78 ms *(measured)* |
| TCP connect to a remote API host | 9.98 ms *(measured)* |
| TLS handshake | 18.95 ms *(measured)* |
| HTTP request → first response bytes | 81.81 ms *(measured)* |
| **total, cold connection to a remote provider** | **118.11 ms** *(measured)* |
| one embedding call, batch of 64, through the gateway | ~50–200 ms *(cited — Aleph's own `ModelCall` rows are the authority; the stack was down)* |
| LLM time-to-first-token | ~0.3–2 s *(cited)* |
| LLM full response, agent turn | ~2–60 s *(cited)* |

**The floor for any remote call is ~30 ms of TCP+TLS before a byte of useful work happens.** A cold
connection to a provider is 118 ms. That is **1,000,000 Aleph capability resolutions.**

### 1.11 The whole budget on one line

Normalizing everything to `ctx.db.query(1)` = 1:

```
in-process capability resolution + call        1
hoisted handle call                            0.09
one structlog line                            54
one Postgres SELECT 1                        330
one JSON round trip of a retrieval result    162
one local process round trip (unix socket)    52
one MCP tool call (in-process, no IPC!)    4,600
one MCP tool call over stdio               7,400
one Postgres FTS query (good selectivity)  4,100
one Postgres FTS query (bad selectivity)  250,000
one embedding batch (network)          ~1,000,000
one LLM turn                          ~100,000,000
```

---

## 2. Where a plugin boundary is free, and where it is fatal

The rule is arithmetic. A boundary is free when **crossings per second × cost per crossing** is a
negligible fraction of the work being done. Concretely, with in-process dispatch at ~117 ns, you can
cross **8.5 million times per second per core** before the boundary costs one full core. With a JSON
boundary at 19 µs you can cross **52,000 times per second.** With an MCP boundary at 865 µs you can
cross **1,150 times per second.**

Now inventory Aleph's real hot paths.

### FREE — put a plugin boundary here without thinking about it

| path | crossings | boundary cost at 117 ns | verdict |
|---|---|---|---|
| **A retrieval request** (`search_corpus`) | ~10 capability resolutions | 1.2 µs against a 0.5–30 ms query | 0.004%–0.2% |
| **An agent turn** | ~50–500 tool/service crossings | 6–60 µs against 2–60 s | ~0.0001% |
| **An ingest job for one document** | ~100 crossings | 12 µs against seconds of fetch+embed | invisible |
| **A ledger write** | 1–2 crossings per mutation | 0.2 µs against a 165 µs transaction | 0.1% |
| **Claim commit / belief patch** | tens per commit | µs against a multi-statement transaction | invisible |
| **HTTP request handling** | tens per request | µs against ≥1 ms of routing + DB | invisible |
| **Scholar / connector fetches** | 1 per network call | 0.117 µs against 100+ ms | 0.0000001% |

Every capability seam Aleph has today falls in this column. **All of them.** The composition root in
`packages/aleph-runtime/src/aleph_runtime/capabilities.py` declares services that are resolved a
handful of times per request and then used against work that costs milliseconds.

### CONDITIONAL — free if you follow the rules, fatal if you don't

| path | crossings | the rule |
|---|---|---|
| **Chunk scoring / re-ranking** | 1,000–100,000 per query | Pass the *whole batch* across the boundary once, not one chunk at a time. A plugin signature of `rank(chunks: Sequence[Chunk]) -> list[float]` is free. `score(chunk) -> float` called 20,000 times is 2.3 ms of pure dispatch, plus whatever the plugin does — and if the plugin is out-of-process it is 20,000 × 6 µs = **120 ms of nothing**. |
| **Embedding batches** | 1 call per 64 texts | Move `list[str]` in and a *buffer* out. Never JSON. §1.5: 12.4 ms vs 2.9 µs per batch. |
| **Chunking / normalization** | 1 per document, ~1,000 per corpus | Boundary per *document* is free. Boundary per *chunk* is 1,000 × 117 ns = 117 µs — still fine in-process, catastrophic out-of-process (6 ms → 865 ms via MCP). |
| **Event fan-out to the interface** | 1 per state change, hundreds/sec | Aleph's `ChangeBroker.publish` (`apps/api/src/aleph_api/realtime.py:85-100`) is already the correct shape: one `put_nowait` per subscriber into a bounded queue, drop on full, never block the publisher. Keep it. Do **not** let plugins subscribe synchronously to it. |

### FATAL — never put a plugin boundary here

| path | crossings | why |
|---|---|---|
| **Token streaming** | 1 per token, ~2,000 per response, ~50/sec sustained | A per-token plugin hook that is merely *in-process and cheap* costs 2,000 × ~180 ns = 0.36 ms/response — survivable. A per-token hook that builds a payload dict first costs 2,000 × 58 ns extra. A per-token hook that any plugin implements with an `await` that suspends costs 2,000 × 13 µs = **26 ms per response of pure event-loop tax**. A per-token hook crossing a process boundary costs 2,000 × 6 µs = **12 ms minimum**, and over MCP, 2,000 × 865 µs = **1.7 seconds**. |
| **Per-row database result processing** | 40–20,000 rows per query | Rows come back from `asyncpg` as a list. Handing plugins the list is free; handing them each row multiplies by N and starts to rival the query itself. |
| **Vector distance computation** | 20,000–200,000 per query | 552 µs for 20k in numpy as one vectorized op. The same work as 20,000 plugin calls to `distance(a, b)` is ~2.3 ms of dispatch **plus** losing SIMD — a 10×+ regression for zero architectural gain. Vector math is not a plugin seam. |
| **Any per-item hook that a plugin may implement asynchronously** | any N | See the loop-tick number. A synchronous per-item hook is 117 ns; the same hook made `async` and actually suspending is 13 µs. The API shape decides this, not the plugin author. |

### The structural answer the reviewed codebases converged on

Every mature harness in the review set solved this the same way, independently:

- **deepseek-harness:** the `llm/stream` waterfall runs **once per model call** and returns an
  `AsyncIterable`; tokens then flow through a bare async generator with zero plugin dispatch
  (`packages/llm/llm/src/index.ts:913-925`, generator at `:844-897`). Plugins negotiate *what stream
  exists*; bytes move without them.
- **hermes-agent:** `on_stream_delta` fires per token and **does not call plugins**. Each
  `(hook, callback)` gets a bounded queue and its own thread with drop-oldest backpressure, and
  return values are ignored (`agent/plugin_stream_hooks.py:120-144`, `_QUEUE_SIZE = 1024`). A slow
  plugin cannot throttle the stream; it loses events.
- **opencode:** plugin code runs at **rebuild** time only; consumers read a materialized `Map`
  (`packages/core/src/catalog.ts:192-197`). On the read path the plugin tax is zero because by then
  the composition *is* a data structure.
- **prime-agent:** the one genuinely per-operation plugin path (`_refreshToolRegistry`) is a
  `Map<string, AgentTool>` built once and rebuilt only on `registerTool`
  (`agent-session.ts:8316-8400`).

Four teams, four languages, one conclusion: **plugins participate in constructing the pipeline; they
do not sit inside it.**

---

## 3. Control plane / data plane

### The principle

Split every plugin interaction into two questions:

- **Control plane:** *what should happen?* Which retriever, which model, which ranking, which
  filters, what budget. Crossed **once per operation** — per query, per model call, per job. Plugins
  belong here. Cost: hundreds of nanoseconds, invisible.
- **Data plane:** *the bytes themselves.* Chunk text, embeddings, tokens, document bodies, images.
  Crossed **once per item** — thousands to millions of times. Plugins must **not** be here, and the
  data must not be copied across any boundary at all.

The mechanism that keeps them apart is simple: **plugins exchange handles and commands, never
payloads.** A handle is a `chunk_id`, a `source_id`, an `asset_id`, a `memoryview`, an open cursor, a
`Sequence[T]` reference — something whose transfer cost is independent of the size of the thing it
names. A payload is text, vectors, or images by value.

All three of the mature harnesses have this, spelled differently:

- deepseek-harness `ctx.attachments` commits image bytes content-addressed and returns an
  `ImageAttachmentRef`; "consumers never persist browser paths, object URLs, provider URLs, or base64
  in session events" (`packages/attachment/attachment/README.md:5`). `ctx.spillStore` writes an
  oversized tool result to a session-scoped file and returns a **locator plus a retrieval hint**, so
  the megabyte never enters history.
- prime-agent: "documents, dataframes, parsed corpora live as Python variables in the kernel; only
  the `repr`/stdout crosses, and that is capped" — `DEFAULT_MAX_OUTPUT_CHARS = 65536`
  (`src/core/kernel/index.ts:31`).
- hermes-agent: tool results must be `str`, oversized ones spill to disk and hand back a reference
  (`tools/tool_result_storage.py`).

Aleph already has the right substrate for this and mostly doesn't know it: **Postgres is the shared
data plane.** Chunks, embeddings, claims, citations and assets already live there with stable UUIDs.
Two plugins that both need a chunk do not need to hand it to each other — they need to agree on its
id.

### What this looks like for Aleph's retrieval path, concretely

Today `search_corpus` is one function
(`packages/aleph-rks/src/aleph_rks/retrieval.py:145-163` → `_hybrid_search` at `:62-142`) that does five
things: build the dense ranking, build the lexical ranking, fuse with RRF, cap per source, and
hydrate `ChunkHit` objects carrying full text. Suppose Aleph wants each of those to be a swappable
plugin — a different fusion, a domain re-ranker, a per-project filter.

**The wrong decomposition** (a payload plane):

```
retriever_plugin.search(query) -> list[ChunkHit]        # each hit carries ~1.2 KB of text
   -> reranker_plugin.rerank(list[ChunkHit]) -> list[ChunkHit]
   -> filter_plugin.filter(list[ChunkHit]) -> list[ChunkHit]
```

In-process this is fine (three calls, ~350 ns). But it is *shaped* so that the moment any one of
those plugins moves out-of-process — the agent authored the reranker, so it must be isolated — the
40 hits × 1.2 KB must be serialized, shipped, and parsed at every hop. Measured: 12.3 µs to dump,
6.7 µs to load, per hop, and it grows with `top_k`. Worse, it forecloses the useful case, because a
reranker that wants to see 500 candidates now moves 600 KB.

**The right decomposition** (a control plane over a shared data plane):

```python
# Control plane — plugins return PLANS and RANKINGS, never text.

class CandidateSource(Protocol):
    """Produce a ranking. Returns ids in rank order, nothing else."""
    async def rank(self, req: RetrievalRequest) -> Sequence[UUID]: ...

class Fusion(Protocol):
    """Combine k rankings into one. Pure, synchronous, ids only."""
    def fuse(self, rankings: Sequence[Sequence[UUID]]) -> Sequence[tuple[UUID, float]]: ...

class Reranker(Protocol):
    """Reorder a candidate set. Receives ids + a read handle, not text."""
    async def rerank(self, req: RetrievalRequest,
                     candidates: Sequence[UUID],
                     chunks: ChunkReader) -> Sequence[UUID]: ...

# Data plane — one hydration, at the end, for the survivors only.
hits = await chunk_reader.hydrate(final_ids[:top_k])
```

Four properties fall out of this shape, and each one is measurable:

1. **Hydration happens once, for `top_k` rows.** Measured: `SELECT id, text FROM chunks WHERE
   id = ANY($1)` for 40 rows costs **90–116 µs**. In the payload design it is paid at every hop.
2. **A ranking is tiny.** 40 UUIDs is 640 bytes as raw 16-byte values, ~1.5 KB as JSON — versus
   ~48 KB for 40 hydrated hits. **A 32–75× reduction in what crosses any boundary**, which is what
   makes it survivable to move a reranker out of process: 40 ids over a unix socket is ~6.5 µs,
   40 hydrated hits is ~19 µs and rising with `top_k`.
3. **`ChunkReader` is a capability handle, not data.** A plugin that needs text for 500 candidates
   calls `chunks.text_for(ids)` and pays for exactly what it reads. An isolated plugin gets a
   *restricted* reader (project-scoped, row-capped) — the same interface, different authority. This
   is the deepseek-harness re-guard idea (`cordis-host-runner/src/guard.ts:640-654`: `ctx.tools.get()`
   returns a schema view, never the live definition carrying `execute`) applied to Aleph's data.
4. **The dense leg never leaves the database.** `DocumentChunk.embedding.cosine_distance(...)`
   (`retrieval.py:94`) runs pgvector's ordering inside Postgres. The query embedding — 4 KB of raw
   floats, 21 KB as JSON — crosses to Postgres exactly once as a bound parameter. It must never cross
   a plugin boundary as JSON (§1.5: 193 µs to serialize one).

Note what this does *not* cost. The plugin indirection itself is three `Protocol` calls at ~11–117 ns
each. Against a 484 µs FTS query, the entire plugin architecture for retrieval costs **0.07%**.

### The same split, applied elsewhere in Aleph

| subsystem | control plane (plugins here) | data plane (handles only) |
|---|---|---|
| Ingest | which connector, which normalizer, which chunker, chunk size policy | document bytes → asset store; chunks → Postgres rows; plugins exchange `source_id` |
| Embedding | which model binding, batch size, which texts need re-embedding | vectors as `bytes`/`memoryview` or straight into the pgvector column; never JSON |
| Retrieval | candidate sources, fusion, rerankers, filters | id rankings; one hydration at the end |
| Belief / claims | which extractor, which reconciler, trust lattice policy | claims, citations, edges as rows; plugins exchange `claim_id` |
| Streaming to the UI | who may *register* a surface renderer; what a pane shows | tokens flow through one generator; the `ChangeBroker` fan-out stays in the kernel |
| Artifacts | which builder, which exporter | rendered bytes → asset store; plugins exchange `asset_id` |

---

## 4. Resolve once, call many

### The goal

Make a plugin call compile down to what it would have been if you had written the monolith:
`self._db.query(x)`. Measured, the target is **10.8 ns** — the hoisted number from §1.2, which is
bit-for-bit a bound method call.

### The mechanism, in Aleph's terms

There are three ways to reach a capability, with a 10× spread:

```python
# (a) resolve per call — 117 ns
async def score(self, ctx, chunks):
    for c in chunks:
        ctx.rks.score(c)          # __getattr__ → frozenset check → Store.get, EVERY iteration

# (b) resolve per operation — 117 ns once, then 10.8 ns each
async def score(self, ctx, chunks):
    rks = ctx.rks                 # one resolution
    return [rks.score(c) for c in chunks]

# (c) resolve at setup — 0 ns on the call path
async def setup(ctx):
    svc = Scorer(rks=ctx.rks, db=ctx.db)   # capabilities captured on the instance
    ctx.provide("scorer", svc)
    yield ...
```

**(c) is the shape the kernel already encourages** and every capability in
`packages/aleph-runtime/src/aleph_runtime/capabilities.py` follows it: `models()` resolves
`HTTP_GATEWAY`, `DB_SESSIONS` and `REDIS` inside `setup` and hands them to the `LiteLLMClient`
constructor (`capabilities.py:275-282`). After boot, `LiteLLMClient.chat()` touches no kernel
machinery at all. **This is why Aleph's plugin overhead in production is currently zero, not 117 ns.**
The 117 ns is what you pay only if you resolve inside a loop.

The remaining risk is that (a) is the *prettiest* of the three — `ctx.rks.score(c)` reads beautifully
— and nothing stops an author writing it. That is exactly cordis's failure mode: its reviewer found
the same convention ("resolve in `apply()`, store on `this`") held by discipline alone, with
`Service` subclasses in its own test suite writing `this.ctx.counter.increase()` per call and
"nothing in the framework warns you."

### What each reviewed codebase does about it

| project | mechanism | result |
|---|---|---|
| **opencode** | **State-as-replay.** Derived state is rebuilt by replaying an ordered array of transforms; consumers read a materialized `Map` (`packages/core/src/state.ts:61-127`, `catalog.ts:192-197`). Plugin code runs at rebuild time only. | Read path is *literally* a compiled monolith. **Best in class.** |
| **opencode** | **Memoize the one genuinely per-operation hook.** `AISDK.language` keys a cache on `${providerID}/${id}/${variant}`; the plugin chain runs once per model, then every call is a `Map.get` (`aisdk.ts:198-227`). Synchronous hooks skip the async machinery via an `Effect.isEffect` check (`:174-182`). | The hot path exits at a cache hit. |
| **opencode** | **Snapshot per turn.** `ToolRegistry.materialize(permissions)` snapshots the effective tool table once per provider turn and returns a closure over it (`tool/registry.ts:106-122`). | Per-call cost is `Map.get`, not a registry walk. |
| **hermes-agent** | **Generation counter as memo key.** A monotonic `_generation` on the tool registry (`tools/registry.py:451-457`) is part of the composed-view cache key (`model_tools.py:363-376`), alongside a config `(mtime_ns, size)` fingerprint. Plugin load/unload invalidates automatically with no explicit hook. | Composed views cost once per *change*, not once per call. |
| **deepseek-harness** | **Framework drives, domain computes.** One subscription to `session/event`; each plugin contributes `{init, apply, view, stateVersion}` and the drive gates downstream work on `Object.is(next, prev)` (`session-projection/src/index.ts:414-415`). | An event concerning nobody costs N no-op calls and zero downstream work. |
| **prime-agent** | Tool registry built once into a `Map`, rebuilt only on `registerTool` (`agent-session.ts:8316-8400`). | Per-call lookup is one `Map.get`. |
| **cordis** | **Nothing.** Every `ctx.service` read runs the Proxy trap, allocates `new Error()` (~822 ns) and mints 2–3 fresh Proxies; there is no memo table anywhere. Hoisting still leaves ~190 ns. | ~1.4 µs per cross-plugin call. **The anti-pattern.** |

### What Aleph should build

Three things, in order of value:

1. **A `materialize` step for every plugin-derived collection.** Anything assembled from plugin
   contributions — the tool catalog, the retriever chain, the reviewer set, the connector registry —
   is built once into a plain `dict`/`tuple` and consumers read *that*. Invalidate it with hermes's
   generation counter (a single `int` on the kernel, bumped by `register_dynamic`, `activate`,
   `deactivate`, `replace`). Consumers cache against `(generation, scope_key)`. This is cheap: the
   kernel already serializes every composition change under one lock
   (`packages/aleph-kernel/src/aleph_kernel/kernel.py:152-166`), so the counter is trivially correct.
2. **Resolve capabilities in `setup`, and let a check say so.** See rule 1 in §8.
3. **Stale-handle invalidation on `replace`.** Resolve-once has one real failure mode: a handle
   captured before a hot swap keeps pointing at the dead implementation. prime-agent solves it by
   making every method start with `assertActive()`, so a captured handle throws a message naming the
   mistake and the fix (`core/extensions/loader.ts:115-161`, `runner.ts:567-625`). opencode solves it
   with identity tokens — a turn snapshots the tool table, each registration carries a fresh `{}`
   token, and settlement compares tokens, so a mid-turn swap returns `Stale tool call` instead of
   silently running new code (`tool/registry.ts:50-61, 93-101`). Twelve lines. Aleph's
   `Kernel.replace` (`kernel.py:240-288`) currently has neither. **Adopt the identity-token version:**
   it costs one attribute compare (~4 ns) on the call path and turns "hot replacement" from a hope
   into a guarantee.

---

## 5. Tiered isolation

Isolation costs are not on a smooth curve. They are three discrete steps, and the numbers say
exactly where to put the boundaries.

| tier | mechanism | per-call cost (measured) | spin-up cost | what it contains |
|---|---|---:|---:|---|
| **0 — core** | in-process, boot manifest, no `PluginId` | **10.8 ns hoisted / 117 ns resolved** | 4.8 µs/capability at boot | nothing. Full process authority. |
| **1 — trusted plugin** | in-process, AST-gated, kernel-mediated context | **10.8–117 ns** | 576 µs gate + 654 µs import | nothing at runtime; the gate only guarantees *loading is not running*. |
| **2 — isolated worker** | separate process, handle-passing protocol | **6–23 µs** (unix socket) | 11–28 ms spawn, paid once | separate address space, separate crash domain, OS-enforceable limits. |
| **3 — sandboxed execution** | `code-runner` subprocess/container, network-partitioned | **11–28 ms** (spawn per call today) | — | credential-less, `cap_drop`, read-only rootfs, internal-only network. |

**The gap between tier 1 and tier 2 is 100–200×. The gap between tier 2 and tier 3 is another
500–2,500×.** That is why "just isolate everything" is not an option, and why "just trust everything"
is not either.

### Where to draw the line

The line is not about who wrote the code. It is about **what the code is on the hot path of.**

**Tier 0 — core, in-process, unremovable.** Everything in the boot manifest today: observability,
database, http clients, redis, models, assets, scholar, realtime, agent store
(`packages/aleph-runtime/src/aleph_runtime/capabilities.py:535-554`). These are resolved a handful of
times per request and used against milliseconds of work. Aleph's guardrail here is already the best
one in the review set and should not be weakened: **a manifest-mounted capability never receives a
`PluginId`, so `deactivate` has no argument value that names it** (`kernel.py:41-50`, `agent_api.py:15-18`). deepseek-harness and opencode both lack this and both admit it —
dsh's `undefine`/`stop` "will remove anything"; opencode "can prove its service graph is well-formed
but nothing stops removing the plugin that supplies the only model provider."

**Tier 1 — trusted plugins, in-process, on the hot path.** Anything whose boundary is crossed more
than ~1,000 times per second, regardless of author: chunkers, normalizers, fusion strategies,
rankers, claim extractors, projection folds. These get the fast path — and therefore they must pass
review and the AST gate, and they must be *structurally* incapable of hot-loop harm:

- The gate (`aleph_kernel.ast_gate`) runs first: 576 µs for a 203-line module. It buys exactly one
  property, which the module's own docstring states honestly — *"loading is not running"*. It is not
  a sandbox.
- Add hermes-agent's second filter, which is better than a static gate and costs almost nothing given
  Aleph's LIFO unwind: **validate by mounting for real in a disposable scope** — temp home, network
  denied, run `setup`, run the probe, then unwind via the effect scope
  (`hermes_cli/plugin_dev.py:34-60`). Measured cost of exactly this cycle on Aleph's kernel:
  **register_dynamic + activate + deactivate = 61 µs.** Observing which capabilities a plugin
  *actually* claims is cheap, reversible, and catches what an AST gate cannot.
- Promote to tier 1 only after a probation period in tier 2. Aleph already has a spawn ledger with
  probation (`packages/aleph-kernel/src/aleph_kernel/spawn_ledger.py`) and nothing uses it for this.

**Tier 2 — isolated workers, off the hot path.** Agent-authored plugins that do real work, and any
plugin that is called at most a few thousand times per second: connectors, exporters, artifact
builders, novel retrievers under evaluation, reviewers. 6–23 µs per crossing is nothing against the
network and database calls these make. Spawn once at activation and keep the process warm — never
per call, which would cost 11–28 ms.

**Tier 3 — the code-runner.** Agent-emitted code that is *executed*, not *loaded*. Aleph's existing
posture (`apps/code-runner/src/aleph_code_runner/executor.py`) is correct and should not change.

### How to keep the fast path fast without making the safe path unsafe

Four rules, each of which the review set validates:

1. **The tier is a property of the *seam*, not of the plugin.** Declare, per extension point,
   the maximum tier admitted: `hot` seams (per-chunk, per-token) accept tier 0/1 only; `warm` seams
   (per-request) accept up to tier 2; `cold` seams (per-job) accept tier 3. An agent-authored plugin
   simply cannot be installed at a `hot` seam. This is the one thing that makes "trusted in-process"
   and "agent-authored" coexist without a policy argument at every review.
2. **One interface, two implementations of the transport.** The plugin's `Protocol` is identical in
   tier 1 and tier 2; only the binding differs (a direct object vs. a proxy over a socket). Promoting
   or demoting a plugin's tier must be a manifest change, not a rewrite. This is what makes probation
   practical.
3. **Capability grants must not leak through return values.** deepseek-harness's sharpest security
   idea: the sandbox façade proxies every injected service so that any returned value that
   `instanceof Context` is refused, and `ctx.tools.get()` returns a *schema view*, not the live
   definition carrying `execute` (`cordis-host-runner/src/guard.ts:669-698`, `:640-654`). Aleph's
   `Context.get` returns the raw object — which is what makes it fast, and also means a capability
   handed to a tier-1 plugin can hand back anything it likes. For tier 1 the answer is *review the
   plugin*; for tier 2 the answer is *the process boundary already prevents it*. What must never
   happen is a tier-2 plugin getting a tier-1 handle "for performance".
4. **Monotonic guards.** dsh again: any guard may deny a call, no guard can force-allow one another
   denied, and `tools.restrict({})` **throws**, because an empty filter is almost always a
   materialized-empty-config bug (`packages/core/tools/src/index.ts:1101-1128`, `:1069-1078`).
   Monotonicity means adding a plugin can only ever narrow permissions, so security composes.
   Aleph should adopt both, including the refusal of the empty filter.

---

## 6. The MCP question

### The measurement

This is the one place where the answer was genuinely surprising, so it was measured directly against
the official SDK (`mcp` 1.28.0), the same version Aleph already has in its dependency tree via
`aleph_scholar.consensus`.

| MCP operation | measured |
|---|---:|
| `stdio` transport: spawn server + `initialize` handshake | **153.29 ms** |
| `stdio` `call_tool("ping", {"x": 1})` — trivial tool, tiny payload | **864.85 µs** |
| `stdio` `call_tool` returning a ~10 KB retrieval result | 823.04 µs |
| `stdio` `list_tools` | 521.39 µs |
| **in-memory streams (no process, no IPC at all): `call_tool("ping")`** | **538.89 µs** |
| in-memory streams: `call_tool` returning ~10 KB | 538.15 µs |

**Read the last two rows twice.** With the process boundary removed entirely — client and server in
the same process, connected by `anyio` memory streams — an MCP tool call still costs **539 µs**. The
subprocess adds only ~325 µs on top. **The cost is the protocol, not the transport.** It is the
JSON-RPC envelope construction, the pydantic validation of request and response models at both ends,
and the several task hops through the session machinery (each of which pays an event-loop tick —
13 µs on this box, less on Linux, but there are many of them).

Note also that payload size barely matters: 864 µs for a 1-integer tool, 823 µs for a 10 KB result.
**MCP's cost is per-call and essentially fixed.** That is actually good news — it means MCP is fine
for coarse operations and terrible for fine ones, which is a clean line to draw.

### The comparison

| boundary | cost | ratio to in-process |
|---|---:|---:|
| in-process hoisted call | 0.011 µs | 0.09× |
| in-process capability resolution + call | 0.117 µs | 1× |
| local unix socket, handle-sized payload | 6.5 µs | 55× |
| raw subprocess stdio JSON-RPC | 10.9 µs | 93× |
| **MCP in-process (protocol overhead alone)** | **539 µs** | **4,600×** |
| **MCP over stdio** | **865 µs** | **7,400×** |
| MCP over HTTP, keep-alive *(inferred: HTTP transport 55 µs + protocol 539 µs)* | ~600–900 µs | ~5,000–7,700× |
| MCP over HTTP, new connection per call *(inferred, + 159 µs)* | ~1,000 µs+ | ~8,500× |

For context, prime-agent hits the pathological version of this: its MCP client "opens a fresh session
per call" (`prime-agent-runtime/src/rlm/mcp_base.py:270-274`) — honest about the trade, but in a loop
over 200 documents that is 200 full handshakes. At 153 ms each, that is **30 seconds of handshaking.**

### Recommendation

**Use MCP when it is the boundary you actually want, and never as an internal decomposition tool.**

**Appropriate:**
- **Exposing Aleph's capability to other agents and tools** — Claude Desktop, another harness, a
  colleague's client. This is what MCP is for and 865 µs against a multi-second agent turn is
  irrelevant (0.04%).
- **Consuming third-party MCP servers.** Aleph already does this for Consensus
  (`aleph_scholar.consensus`). One call per scholarly search, against a network fetch that costs
  100 ms+. 865 µs is 0.9%. Fine.
- **Coarse, agent-facing operations**: `search_corpus`, `ingest_source`, `compose_report`,
  `get_claim`. Called single-digit to low-double-digit times per turn. **Budget: keep MCP-exposed
  operations under ~1,000 calls per turn and the overhead stays under 1 second.**
- **A stable, versioned public contract** where you *want* schema validation at the boundary and the
  ability to evolve either side independently.

**Not appropriate:**
- **Anything per-chunk, per-token, per-row, or per-embedding.** 20,000 chunks × 865 µs = **17
  seconds**. The same work in-process is 2.3 ms.
- **Internal plugin-to-plugin communication.** If two Aleph plugins in the same deployment need to
  talk, they should share a heap or share Postgres. Routing them through MCP costs 7,400× and buys
  nothing — the schema validation is re-checking types Python already enforced. This is
  deepseek-harness's rule stated as policy: *"Trust TypeScript at typed same-process boundaries. Do
  not add runtime validation… validate at parser/config, queued, model/tool JSON, durable/file,
  worker, process, and wire boundaries"* (`AGENTS.md:115`). Seven named boundaries; everything
  between them is a plain typed call.
- **As the tier-2 isolation transport.** If Aleph wants isolated in-deployment plugins, a plain
  length-prefixed protocol over a unix socket costs **6.5 µs** versus MCP's 865 µs — **133× cheaper**
  for the same process boundary. MCP's price buys interoperability with clients Aleph does not
  control, which is worth nothing when both ends are Aleph.
- **Startup discovery on the critical path.** 153 ms per server handshake. Cache the schemas on disk
  and start the server lazily — hermes-agent's `tools/mcp_schema_cache.py` does exactly this, "so
  Hermes can register MCP tools into the agent snapshot without spawning the stdio child process at
  idle dashboard startup," keyed by server name + a fingerprint of the connection config.

**The design rule:** MCP is an *external* interface. Draw it at the edge of Aleph and let everything
inside be in-process calls and Postgres. If an internal capability later needs to be exposed over
MCP, wrap it — the wrapper costs 865 µs and the internal callers keep paying 117 ns.

---

## 7. Startup and lazy loading

### Why this matters more than it looks

Aleph is meant to be **restarted rarely and reloaded often.** That inverts the usual priority. If the
process restarts once a week, a 5-second boot is irrelevant. But the whole product thesis is that the
agent adds, swaps and removes capability *while the system runs* — so the cost that matters is
**time-to-first-useful-work after a composition change**, and the failure that matters is a plugin
count that silently drags boot time up until someone starts avoiding restarts.

### What Aleph's startup actually costs today

Measured with `python -X importtime -c "import aleph_runtime"`:

| import | cumulative ms |
|---|---:|
| **`aleph_runtime` (the composition root)** | **350** |
| ` └ aleph_runtime.capabilities` | 339 |
| `   └ aleph_kernel` | **127** |
| `     └ aleph_kernel.effects → aleph_observability.tracing → aleph_observability` | 125 |
| `       └ langfuse` | **105** |
| `   └ aleph_scholar` | 85 |
| `     └ aleph_scholar.consensus → mcp` | **81** |
| bare interpreter start (`python -c pass`) | ~11 |

**Importing Aleph's kernel costs 127 ms, of which 125 ms is Langfuse.** The chain is
`aleph_kernel/effects.py:30` → `from aleph_observability.tracing import start_span`. The kernel — the
thing whose whole job is composition — cannot be imported without pulling in an observability SDK,
an HTTP client stack, and a batch-evaluation module (`langfuse.batch_evaluation`, 37 ms on its own).
Similarly, `aleph_runtime.capabilities` imports `aleph_scholar`, which imports `mcp` (81 ms) because
of the Consensus client, whether or not any Consensus call is ever made.

This is not a crisis at 350 ms. It is a **trend line**, and it is the exact trend line that makes
plugin systems slow: every new capability adds its dependency closure to the import of the
composition root, whether or not it is used. Twenty plugins each pulling a 100 ms SDK is a 2-second
boot that nobody decided on.

### The composition machinery itself is free

| operation | measured |
|---|---:|
| `Kernel.boot()` of 10 chained capabilities (trivial setup+probe) | 0.05 ms — **4.9 µs/capability** |
| `Kernel.boot()` of 50 chained capabilities | 0.23 ms — **4.7 µs/capability** |
| `Kernel.boot()` of 200 chained capabilities | 0.96 ms — **4.8 µs/capability** |
| `register_dynamic` + `activate` + `deactivate`, one capability on a warm kernel | **60.9 µs** |
| `ast_gate.check_source` on a 203-line plugin | 576 µs |
| `importlib` `exec_module`, fresh 203-line plugin (cold `.pyc`) | 654 µs |
| `importlib` `exec_module`, warm `.pyc` | **27.6 µs** |

**Flat at 4.8 µs per capability out to 200.** Aleph's kernel could mount a thousand capabilities in
5 ms. The topological sort, the effect scopes, the contexts, the probe gate — all of it is free.
**Everything that makes boot slow is inside `setup` and `probe`, not in the kernel.** Which means
the levers are:

1. **Import lazily; declare eagerly.** This is hermes-agent's strongest structural idea and the
   direct answer to "don't get crazy slow": *"the manifest declares the surface and the host
   registers a deferred loader; the Python (and its heavy SDK) is imported only on first real use,
   while the plugin's cheap client tools register with zero import"*
   (`hermes_cli/plugins.py:4437-4523`). Capability becomes *visible and routable* without paying
   import cost, so **plugin count stops driving startup time.**
   For Aleph specifically, three changes worth making now:
   - Move `start_span` behind a lazy import or a no-op shim so `aleph_kernel` does not transitively
     import Langfuse. Saves ~105 ms and — more importantly — decouples the kernel from a heavy
     optional dependency.
   - Make `aleph_scholar.consensus` import `mcp` inside the function that uses it. Saves ~81 ms.
   - Declare capabilities in the manifest with an import path, and import the module in `setup`, not
     at `capabilities.py` module scope.
2. **Probes must be cheap and parallel-safe.** The probe gate is load-bearing and correct — a
   capability that cannot answer a live query must not come up. But probes are where boot time goes:
   a pooled `SELECT 1` is 130 µs, an unpooled `connect + SELECT 1 + close` is **1,592 µs**, and the
   `models()` probe reaches the gateway over the network (§1.10: ~30 ms of TCP+TLS minimum, 118 ms
   cold). Probes must reuse the connection the capability just built — never open their own.
3. **Boot is serialized under one lock** (`kernel.py:152-166`), which is the right simplification
   for correctness. At 4.8 µs of kernel overhead per capability that costs nothing; what it does cost
   is that independent capabilities cannot probe in parallel, so N network probes are N×30 ms
   serially. If boot time ever becomes a problem, activate independent subtrees concurrently — the
   topological order already identifies them. cordis's answer to the general problem is the
   **inertia lock** (`fiber.ts:399-458`): one in-flight transition *per plugin*, so a slow teardown
   blocks only its own subtree, not all composition. Aleph's process-wide `asyncio.Lock` is
   simpler and, today, correct; note it as the thing to revisit if a probe ever gets slow.
4. **Reload beats restart, and Aleph is already close.** A dynamic capability cycle is **61 µs** and
   a warm module re-import is **27.6 µs**. Reloading a plugin should cost under a millisecond
   end to end. The one gap: an agent-authored plugin's *first* load pays the AST gate (576 µs) plus a
   cold import (654 µs), which is still nothing. **There is no performance reason for Aleph's
   agent-facing kernel half to be unreachable.** It is 61 µs of machinery that already works.
5. **Keep the dynamic path and the boot path the same code.** cordis's best structural lesson: its
   own CLI boots via `ctx.plugin(Loader)` plus one config entry (`packages/core/bin.js:1-16`), so the
   mechanism an agent would use is the mechanism that starts the process — and therefore cannot rot.
   Aleph's audit found the boot half well built and the agent half unreachable *precisely because
   they are different paths*. `mount_manifest` should call the same `register_dynamic` +
   `activate` path, with "protected" set by the loader rather than by a different entry point.

---

## 8. Design rules

Each rule is stated so that a violation is **visible in a diff**. Where a rule can be mechanized,
the check is named — Aleph's own CLAUDE.md is emphatic that rules held by prose alone do not hold.

**Rule 1 — Resolve capabilities in `setup`, never inside a loop.**
A `ctx.<name>` or `ctx.get(...)` inside a `for`/`while` body, a comprehension, or a function called
per-item is a defect. Hoist to a local, or capture on the instance at construction.
*Review test:* grep the diff for `ctx.` inside an indented loop body.
*Why:* 117 ns → 10.8 ns, and it is the difference between "the kernel costs something" and "the
kernel costs nothing."
*Mechanizable:* an AST check for `Attribute(value=Name('ctx'))` or `ctx.get(...)` inside a loop, in
the same style as `scripts/check-graph-state-keys.sh`.

**Rule 2 — A plugin interface takes a batch or a handle, never a single item.**
`rank(chunks: Sequence[Chunk]) -> list[float]`, not `score(chunk) -> float`.
`embed(texts: Sequence[str])`, not `embed(text: str)`.
*Review test:* does any `Protocol` method's name suggest it is called once per row, chunk, token, or
vector? If yes, it must take a sequence.
*Why:* it is the single decision that determines whether the seam can ever be moved out of process.
A batch interface survives promotion to tier 2 (one 6 µs crossing); a per-item interface does not
(N × 6 µs).

**Rule 3 — Nothing bigger than a handle crosses a plugin boundary that could ever be
out-of-process.**
Ids, `memoryview`s, cursors, locators — never chunk text, never vectors, never document bodies,
never base64.
*Review test:* look at the return type. If it contains `str` that could be a kilobyte, or
`list[float]` that is an embedding, it is a payload.
*Why:* §1.5 — one embedding is 74 ns as bytes and 193,000 ns as JSON.

**Rule 4 — Zero listeners must cost one branch.**
Every fan-out site checks `if not listeners: return` **before** building any payload.
*Review test:* is there a dict/object literal, an f-string, or a `.model_dump()` above the emptiness
check? That is the bug.
*Why:* 19.5 ns → 13.1 ns for the check itself, but the real cost is the payload you avoid building
(58 ns/dict, 713 ns if it is JSON-encoded) on a per-token path. This is prime-agent's live defect
(`runner.ts:674-676`) and hermes-agent's explicit, documented rule
(`hermes_cli/plugins.py:285-288`: *"every call site short-circuits on has_hook(), so when nothing
subscribes no payload is built"*).

**Rule 5 — No reflection on a call path.**
`inspect.signature`, `typing.get_type_hints`, `dataclasses.fields`, `isinstance` chains over long
unions: compute once at registration, store the answer on the registration record.
*Review test:* does the diff `import inspect` or `import typing` inside a function that runs per
call?
*Why:* 2,831 ns — 24× a full capability resolution — for information that was static at import time.
This is hermes-agent's `_invoke_hook_callback` bug, verbatim.

**Rule 6 — Hot paths may only fire *queued* observers.**
Per-token and per-chunk hooks put onto a bounded queue with drop-oldest backpressure; return values
are ignored; a plugin can never make the producer wait.
*Review test:* can a plugin's coroutine be `await`ed by the token loop? If yes, reject.
*Why:* a suspending `await` costs a loop tick — 13 µs here, ~1 µs on Linux — so 2,000 tokens is
2–26 ms of pure scheduling per response. Aleph's `ChangeBroker.publish`
(`apps/api/src/aleph_api/realtime.py:85-100`) is already exactly this shape; make it the pattern, and
give plugins no other way onto a hot stream. hermes-agent institutionalizes it
(`agent/plugin_stream_hooks.py:120-144`, `_QUEUE_SIZE = 1024`).

**Rule 7 — One task per batch, never one task per item.**
No `asyncio.create_task` / `gather` inside a per-chunk or per-row loop.
*Review test:* is `create_task`, `gather`, `to_thread` or `TaskGroup.start_soon` inside a loop whose
iteration count scales with corpus size?
*Why:* 99 ns per callback when batched into one tick, 13,320 ns when each gets its own — a 134×
penalty for granularity alone.

**Rule 8 — Anything assembled from plugin contributions is materialized once and read as data.**
Build the tool catalog, retriever chain, connector registry, reviewer set into a plain
`dict`/`tuple`; consumers read that. Cache it against a kernel generation counter bumped by every
composition change.
*Review test:* does a read path walk the plugin registry? It should read a materialized view.
*Why:* opencode's central insight — *"on the read path the plugin tax is literally zero, because by
then it is a compiled constant"* (`state.ts:61-127`). Combined with rule 1, this makes plugin
overhead in steady state genuinely, measurably zero.

**Rule 9 — Every capability declares its seam class, and the seam class caps the isolation tier.**
`hot` (per-chunk/per-token): tier 0–1 only, in-process, batch interfaces, no agent-authored code.
`warm` (per-request): up to tier 2. `cold` (per-job): up to tier 3.
*Review test:* a new extension point without a declared class, or an agent-authored plugin installed
at a `hot` seam.
*Why:* it converts "should this be isolated?" from a judgement call made per plugin into a property
of the architecture, and it is what lets tier-1 speed and agent-authored code coexist.

**Rule 10 — MCP is an edge protocol.**
No internal Aleph-to-Aleph communication goes over MCP. MCP-exposed operations are coarse (a
retrieval, an ingest, a report), and their schemas are cached on disk so no server is spawned at
startup.
*Review test:* does the diff add an MCP client call inside a loop, or an MCP server spawn on the boot
path?
*Why:* 865 µs per call and 153 ms per handshake — 7,400× and 1,300,000× an in-process call.

**Rule 11 — Import lazily, declare eagerly.**
A capability's manifest entry declares its name, provides, requires and config without importing its
implementation. Heavy SDKs are imported inside `setup`, never at module scope of the composition
root.
*Review test:* does a new top-level `import` in `aleph_runtime/capabilities.py` (or any module it
imports) pull a package that is not needed on every boot?
*Why:* the composition root already costs 350 ms, 105 of which is Langfuse reached through
`aleph_kernel.effects`. Without this rule, plugin count becomes boot time.
*Mechanizable:* a CI check asserting `python -X importtime -c "import aleph_runtime"` stays under a
budget, and that `import aleph_kernel` stays under, say, 30 ms.

**Rule 12 — A handle minted before a hot swap must fail loudly, not silently.**
`Kernel.replace` invalidates outstanding handles; a call through a stale handle raises a message
naming the swap and the fix.
*Review test:* does `replace` leave any way for a captured reference to keep working against the old
implementation?
*Why:* resolve-once (rules 1 and 8) creates this exposure by design. opencode's identity token
(`tool/registry.ts:50-61, 93-101`) costs one pointer compare — ~4 ns — and prime-agent's
`assertActive()` (`loader.ts:115-161`) costs one boolean check. There is no performance excuse for
not having it.

---

## 9. The answer, in one paragraph

Aleph will not get slow because it is a plugin system. Aleph's plugin dispatch costs 117 nanoseconds
resolved and 11 nanoseconds hoisted — against a 36-microsecond database round trip, a 484-microsecond
retrieval query, and a multi-second LLM turn. The kernel mounts capabilities at 4.8 µs each and
scales flat to 200. The risks are all elsewhere and all specific: serializing what should have stayed
a handle (an embedding is 2,600× more expensive as JSON than as bytes); creating an asyncio task per
item instead of per batch (134× penalty); putting a plugin hook inside the token stream or the chunk
loop rather than around it; reaching for MCP as an internal decomposition tool (7,400×); and letting
each new plugin's dependency closure land in the composition root's import (already 350 ms, 105 of it
Langfuse imported through the kernel). Every one of those is a *shape* you can see in a diff, which
is why §8 is the operative section of this document. Build the plugin architecture. Keep the data
plane out of it.

---

## Appendix A — reproducing these numbers

Benchmark scripts, written for this review, in the session scratchpad
(`/private/tmp/claude-501/-Users-jpmullins-Documents-code-aleph/64bacac9-ec36-472e-ba68-4e9e981130f5/scratchpad/`).
They are throwaway; if they are worth keeping, they belong in `packages/aleph-evals` with the
retrieval eval, so the numbers can be re-derived when the kernel changes.

| script | what it measures |
|---|---|
| `bench_inproc.py` | the Python floor, Aleph's real `Context`/`Store` path, dispatch shapes, payload serialization, instrumentation |
| `bench_async.py`, `bench_loop.py` | coroutine/task/lock/queue costs; isolates the event-loop tick to a selector syscall |
| `bench_ipc.py` | unix socketpair, TCP loopback, subprocess stdio JSON-RPC, HTTP loopback, process spawn, shared memory |
| `bench_pg.py`, `bench_pg2.py` | real Postgres 17 over 20,000 chunks: round trips, PK lookups, transactions, pooling, and FTS `ts_rank` at three selectivities |
| `bench_vec.py` | embedding transport (JSON vs bytes), brute-force cosine at 20k and 200k, per-token payload costs |
| `bench_boot.py` | `Kernel.boot()` scaling, dynamic register/activate/deactivate, `ast_gate.check_source`, `importlib` cold vs warm |
| `bench_mcp.py`, `bench_mcp_mem.py` | real MCP SDK 1.28.0: stdio handshake and `call_tool`, plus the same calls over in-memory streams to isolate protocol cost from transport cost |

**Two measurements worth re-running in CI as regression guards**, because they are the two that would
silently degrade: `ctx.db.query(1)` (must stay ≤ ~150 ns, and hoisted must stay at bare-method-call
cost) and `python -X importtime -c "import aleph_runtime"` (must stay under a declared budget).

## Appendix B — licenses

None of the reviewed projects' code appears in this document except as short quoted excerpts for
identification and critique. All five are MIT:

| project | license | note |
|---|---|---|
| cordis (cordiverse/cordis) | MIT, © 2021-present Shigma | v4.0.0-rc.8; its own README warns the API is unstable. |
| deepseek-harness | MIT | vendors Cordis; `0.1.0-rc.7`, explicit "no compatibility promise". |
| prime-agent (PrimeIntellect-ai) | MIT | fork of Mario Zechner's `pi`. |
| opencode | MIT | mid-rewrite; three coexisting plugin systems. |
| hermes-agent (NousResearch) | MIT | ~2.09M lines, mostly hand-written. |

Aleph's standing constraint applies unchanged: **reference implementations are read, not depended
on.** Nothing here should be vendored. The ideas adopted — the epoch/generation counter, the
materialized read path, queued hot-path observers, manifest-eager/module-lazy loading, identity
tokens on registrations, monotonic guards, the disposable-scope validation mount — are all
reimplementable in a few dozen lines each, and any ported code carries a `NOTICE`
(see `packages/aleph-belief/NOTICE`).
