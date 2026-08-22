# Operations

## Bootstrap

```bash
cp deploy/compose/.env.example deploy/compose/.env
# set INSIGHTS_LITELLM_API_KEY; review the rest
./scripts/bootstrap-local.sh
```

`bootstrap-local.sh` brings up the compose stack, waits for health, and runs migrations. Endpoints:
web `:5173`, api `:8000`, copilot-runtime `:4000`, Langfuse `:3000`.

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
docker compose -f deploy/compose/docker-compose.yml logs -f aleph-api
docker compose -f deploy/compose/docker-compose.yml down
docker compose -f deploy/compose/docker-compose.yml --profile s3 up -d   # opt-in object store
./scripts/verify-gateway.sh                                              # LLM gateway reachability
```

## Running against any OpenAI-compatible endpoint

Aleph serves no models. It connects out to whatever OpenAI-compatible endpoint is
configured — LiteLLM, Ollama, vLLM, Bedrock behind a proxy — and discovers what
that endpoint serves at runtime. It ships no model list and no price list.

```
# deploy/compose/.env
LITELLM_BASE_URL=https://your-gateway.example.com
LITELLM_API_KEY=...
```

Two things to know before pointing it somewhere new.

**`LiteLLMClient` posts chat *and* embeddings to the same base URL.** A bare vLLM
serves chat only, so an endpoint without an embeddings route breaks ingest at the
embed step. That failure is worse than it sounds: chunks are written only *after*
the embed call returns, so a missing embedder also kills the lexical leg of
retrieval, which needs no model at all. The source ingests, produces no chunks,
and becomes invisible to both legs. Put something in front that serves both.

**The bound model name must match what the endpoint actually serves.** This is
not hypothetical — production currently has `document_chunks` at 0 rows against
75 sources because the profile binds `titan-embed-v2` while the gateway serves
`titan-embed-text-v2`. Check with:

```bash
curl -s localhost:8000/v1/gateway/models | python3 -m json.tool
```

> Earlier versions of this document described a bundled gateway service under
> `deploy/`. There is none, and there was none by the time the sentence was
> written. Aleph serves no models.


## Migrations

Alembic lives in `apps/api`. Never edit an existing revision; add a new one.

```bash
cd apps/api
uv run alembic upgrade head
uv run alembic check                              # must produce no diff — CI enforces
uv run alembic revision -m "<slug>" --autogenerate
```

File pattern: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.

### The downgrade has to run, not just exist

Every revision carries a `downgrade()` body and for a long time not one had ever been executed:
`alembic check` guards the FORWARD path, and the reverse path was guarded by nothing. A `downgrade`
that has never run is a rollback plan you find out about during the incident.

```bash
./scripts/check-migration-roundtrip.sh              # upgrade → downgrade -1 → upgrade → alembic check
./scripts/check-migration-roundtrip.sh --full       # all the way down to base and back up
```

It runs against a **scratch database it creates and drops** — downgrading the development database
would destroy the corpus, and a check nobody dares run is not a check. It needs `DATABASE_URL` to
point at a Postgres it may create a database on, and it needs `pg_available_extensions` to list
`vector`. The `--last` mode is wired into CI (`.github/workflows/ci.yml`), and
`scripts/_acceptance/self_check.sh` proves it catches a broken downgrade by mutating the newest
revision — it resolves which revision that is at run time, because a probe naming a fixed filename
stops testing the thing the check executes the moment someone adds a migration.

## Backup and restore

**A backup nobody has restored from is a file, not a backup.** Two scripts and one drill:

```bash
./scripts/backup.sh                                  # -> data/backups/aleph-<utc timestamp>/
./scripts/backup.sh --dry-run                        # what it would do; writes nothing
./scripts/restore.sh <backup-dir> --into aleph_scratch
./scripts/restore.sh <backup-dir> --into aleph_scratch --dry-run
uv run python scripts/_acceptance/restore_drill.py   # back up the live DB, restore, verify, drop
```

`backup.sh` writes a directory, not a file: `database.dump` (pg_dump custom format),
`manifest.json`, `toc.txt`, and — when docker is reachable and the `aleph_assets` volume exists —
`assets.tar.gz` with a `assets.sha256` listing. Pass `--no-assets` to skip the volume, `--out DIR`
to choose where, `--database-url URL` to name the database. With no arguments it reads
`DATABASE_URL`, then `ALEPH_DATABASE_URL`, then `deploy/compose/.env` — because the password lives
in that file and nowhere else, and a backup script that demands an unset environment variable is a
backup script that does not get run.

`restore.sh` **verifies and can say no.** It refuses to restore over the database it is connected
to, refuses to restore into a database that already exists unless you pass `--drop-existing`,
refuses a bare `.dump` with no manifest (there is nothing to verify it against), and after the
restore compares the result to the manifest: every table's exact row count, every table's content
digest, the pgvector embeddings component by component, the extension set and its versions, the
alembic revision, and every trigger. Then it fires a real `UPDATE` and a real `DELETE` at each
append-only table inside a rolled-back transaction and requires both to be refused. Only then does
it print OK. Anything else exits 1 and says which table.

### The three things that make a restore a trap

- **pgvector.** The dump carries `CREATE EXTENSION vector`, but the extension has to be *available*
  in the target cluster and the restoring role has to be allowed to create it. If that one statement
  fails, the `vector` type never exists and every table with an embedding column is silently absent
  from the result. `restore.sh` checks `pg_available_extensions` before it creates anything, and
  pre-creates the extension so a role that may not create extensions still works when a DBA has
  already installed it. It also asserts the dump's own table of contents contains the entry, at
  backup time, where the fix is cheap.
- **The append-only triggers.** Ten of them, on five tables — `action_ledger_events`,
  `wiki_revisions`, `interactive_card_versions`, `hypothesis_versions`, `artifact_versions`. In a
  custom-format dump they are **post-data**, so a full restore into an empty database recreates them
  *after* the COPY: the data loads and the triggers come back enabled. That is a property of this
  restore path, not of restores generally — a `pg_restore --data-only --disable-triggers` that dies
  part way leaves them `tgenabled='D'`, which is a database that looks complete and has quietly lost
  the guarantee the ledger's evidentiary value rests on. So the verification asserts `tgenabled` and
  then exercises the trigger for real. Replacing `ledger_immutable()`'s body with `RETURN NEW` leaves
  the trigger definition byte-identical; only firing an UPDATE catches it.
- **A live source that will not hold still.** The manifest is computed *inside the dump's own
  snapshot* — `pg_export_snapshot()`, handed to `pg_dump --snapshot=`, with the inventory query in
  the same REPEATABLE READ transaction. Counting rows from a second connection a moment later counts
  a different database: ingest and the research loop write continuously, and `document_chunks` was
  measured moving by thousands of rows inside a minute. A verifier that fails for that reason is a
  verifier people turn off.

The content digests are only comparable if both sides render rows the same way, so `DateStyle`,
`TimeZone`, `extra_float_digits` and `bytea_output` are pinned and **recorded in the manifest**. If
they ever differ, `restore.sh` reports the restore as UNVERIFIED rather than as bad — a broken
instrument and a broken restore are different findings.

### The drill

```bash
export DATABASE_URL=...        # the live database
uv run python scripts/_acceptance/restore_drill.py
```

Backs up the running database, restores it into `aleph_restore_drill_<pid>`, runs the whole
verification above, re-derives the **ledger hash chain** with the production verifier
(`aleph_db.repos.ledger.verify_project_chain`) on *both* databases, and drops the scratch database
on the way out including after a failure. Roughly 15 seconds against the current corpus.

The chain leg requires the two sides to **agree**, not the restore to verify clean, and that is
deliberate. The development database contains projects whose chain genuinely does not verify: every
run of `tests/integration/test_ledger_immutability.py::test_tampering_is_detectable` forges a
`chain_hash` into a fresh project and leaves it behind. A drill that demanded a clean chain would be
red forever for a reason that has nothing to do with backups, and a check that is red for an
unrelated reason gets ignored. Requiring agreement still catches everything a restore can do wrong:
move a divergence, introduce one, or silently repair one.

### Restoring the assets

The asset store is a docker volume, not rows.

```bash
./scripts/restore.sh <backup-dir> --into aleph_scratch --assets-volume aleph_assets_scratch
```

It refuses a volume that already has files unless you pass `--force`, because unpacking a backup over
live assets mixes two generations of files and the result passes every count. After unpacking it
re-hashes every file and compares against the listing taken at backup time.

### What is NOT covered

- **No schedule.** Nothing runs `backup.sh` on a timer; it is a command an operator runs. There is no
  retention policy, no offsite copy and no quota.
- **No point-in-time recovery.** This is dump-and-restore. WAL archiving to the object store is the
  next step, not a thing that exists.
- **No disk-usage reporting.** `postgres-data` and the asset root can fill the host and nothing
  reports it before they do — `/readyz` carries no bytes-used field. That belongs with the metrics
  work.
- **Redis is not backed up**, deliberately: it holds queues and the sandbox bus, both reconstructible.
  Anything durable is in Postgres.

## Rotating a secret

Aleph holds two long-lived secrets and they do different jobs. They used to be
one value, which meant the ordinary response to a leak — rotate it — silently
and permanently destroyed every stored connector credential.

| setting | what it protects | rotating it costs |
|---|---|---|
| `ALEPH_AGENT_TOKEN_SECRET` | signs the short-lived tokens workers use to call back into the API | in-flight tokens; restart and they re-mint |
| `ALEPH_CREDENTIAL_MASTER_KEY` | encrypts every stored connector credential (real third-party API keys, the Consensus OAuth grant) | a re-encryption pass — the procedure below |

Both are required at boot. A value under 32 bytes, or the `.env.example`
placeholder, is refused by the API and by the workers with a message naming the
setting. Neither is padded: padding is what let a short secret through the
cipher's own length guard before this was split.

### Rotating the signing secret

Change `ALEPH_AGENT_TOKEN_SECRET` and restart. Nothing else is affected —
credentials are keyed independently, and
`packages/aleph-connectors/tests/test_key_rotation.py::test_rotating_the_signing_secret_does_not_destroy_credentials`
is the test that says so.

One exception: credentials written *before* the keys were split carry
`key_version = 'v1'` and really were encrypted from the old signing secret. If
any remain (check with the dry run below), copy the current signing secret into
`ALEPH_CREDENTIAL_LEGACY_KEY` **before** changing it, or those rows become
unreadable.

### Rotating the credential master key

Four steps, in this order. Doing 1 and 4 together is the failure mode: there is
no moment at which both keys are readable, so any row not re-encrypted in the
gap is gone.

```bash
# 1. Install the new key alongside the old. In deploy/compose/.env:
#      ALEPH_CREDENTIAL_LEGACY_KEY=<the key currently in use>
#      ALEPH_CREDENTIAL_MASTER_KEY=$(openssl rand -hex 32)
docker compose -f deploy/compose/docker-compose.yml up -d api workers

# 2. Verify BEFORE anything is destructive. Reports how many rows are still on
#    the old key and whether every one of them can actually be opened.
docker compose -f deploy/compose/docker-compose.yml exec api \
  python -m aleph_connectors.reencrypt --dry-run

# 3. Re-encrypt. Each row moves and is ledgered individually, so one bad row is
#    reported and skipped rather than aborting the pass.
docker compose -f deploy/compose/docker-compose.yml exec api \
  python -m aleph_connectors.reencrypt

# 4. Only when a second dry run reports 0 rows: remove
#    ALEPH_CREDENTIAL_LEGACY_KEY from .env and restart.
```

Exit codes: `0` nothing left to do or everything moved · `1` at least one row
could not be opened with any configured key — **do not remove the old key** ·
`2` misconfiguration (no `DATABASE_URL`, no master key).

`--project-id <uuid>` limits the pass to one project. Every move writes a
`connector_credential.reencrypt` action-ledger event carrying the from/to key
versions and nothing else — never the plaintext, never key material.

### Langfuse

Langfuse has its own three secrets (`LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_SALT`,
`LANGFUSE_ENCRYPTION_KEY`). Compose used to pass `ALEPH_AGENT_TOKEN_SECRET` as
all three, which put Aleph's agent-signing key inside a third-party container's
session cookies and at-rest encryption. Changing `LANGFUSE_ENCRYPTION_KEY` on a
populated Langfuse makes *its* stored API keys undecryptable — the same lesson,
one layer out — so set the three to the value already in use when upgrading an
existing stack, and to fresh random values on a new one.

## Surviving a crash, a dead gateway, and a big upload

### Restart policy — and the one thing it deliberately does not do

Every long-running service carries `restart: unless-stopped`; the three one-shots
(`migrate`, `langfuse-db`, `minio-buckets`) carry `restart: "no"`, because a
restart policy on a job whose contract is "exit 0 once" retries a broken
migration forever while everything waiting on `service_completed_successfully`
waits on an exit code that never comes.

**`docker kill aleph-api-1` will not bring it back, and that is correct.** A kill
or stop issued through the Docker API is a *manual* stop, and `unless-stopped`
exists precisely to honour one. Measured on this stack: `docker kill` leaves the
container `Exited (137)` with `RestartCount` still `0`, indefinitely. Use
`docker compose up -d api` to bring it back.

What the policy does cover is every failure the operator did not ask for.
Measured, with the API capped below its working set so the kernel killed it:

```
docker update --memory 48m --memory-swap 48m aleph-api-1
# State.OOMKilled=true  ExitCode=137  RestartCount 0 -> 1  status=restarting
docker update --memory 2g  --memory-swap 2g  aleph-api-1
# back to healthy, /readyz 200
```

Host reboot is the same mechanism: the daemon restarts everything that was not
manually stopped.

### Readiness, and why the model endpoint is not in it

`/readyz` votes on the dependencies Aleph owns — Postgres, Redis, the asset
store. The model gateway is reported and does **not** vote. This is what lets the
stack start when your models are unreachable:

```bash
LITELLM_BASE_URL=http://127.0.0.1:1 \
  docker compose -f deploy/compose/docker-compose.yml up -d --wait   # exits 0
curl -sf localhost:8000/readyz | jq .checks.litellm_gateway
# { "ok": false, "checked_age_s": 12.5, "max_age_s": 30.0,
#   "last_success_age_s": null, "stale": true, "in_verdict": false }
```

The gateway leg is cached so a healthcheck every 15s is not an outbound request
every 15s, and it publishes the age of its answer so the cache cannot hide a
dead endpoint. Measured: with the gateway killed under a running API, the leg
flipped to `ok: false` after **29 seconds** against its 30-second bound.

`/readyz?strict=1` folds the gateway into the verdict and returns 503 when it is
down. Nothing that restarts a container is wired to it — it is the endpoint for
"can this stack answer a question right now".

### Resource limits

`mem_limit` is set equal to `memswap_limit`, which is the only way compose can
say *no swap*; `deploy.resources` cannot express it. Measured at idle on this
stack:

| service | idle | cap | why that cap |
|---|---|---|---|
| `api` | 210 MiB | `API_MEM_LIMIT`, 2g | reads an upload into memory before it reaches the store |
| `workers` | 130 MiB | `WORKERS_MEM_LIMIT`, 2g | normalizes whole documents in memory during ingest |
| `web` (dev image) | **1.07 GiB** | `WEB_MEM_LIMIT`, 3g | Vite dev server + esbuild, watcher on polling |
| `copilot-runtime` | 122 MiB | `RUNTIME_MEM_LIMIT`, 512m | a thin SSE proxy; hitting this is a leak |
| `code-runner` | 26 MiB | 1g | agent-written code |

`postgres`, `redis` and `runner-redis` are deliberately uncapped: Postgres
manages its own memory through `shared_buffers`/`work_mem`, and a cgroup limit
below those produces OOM kills mid-query rather than backpressure. Cap them only
alongside tuning them.

A cap below the working set is worse than no cap, and the restart policy hides
it: `web` at 1g was OOM-killed on boot, restarted, and crash-looped while
`docker compose ps` reported `Up 1 second (health: starting)` forever.

### Serving the built UI

The compose stack runs `apps/web/Dockerfile.dev` — a Vite dev server with the
source bind-mounted, so an edit on the host is live in the browser.
`apps/web/Dockerfile` is the production image: a compiled bundle behind nginx,
no Node at runtime, running as uid 101.

`import.meta.env.VITE_*` is substituted at **build** time, so the API URL is
baked into the bundle and setting it in the container's environment does
nothing:

```bash
docker build -f apps/web/Dockerfile \
  --build-arg VITE_API_BASE_URL=https://aleph.example.com \
  --build-arg VITE_COPILOT_RUNTIME_URL=https://aleph.example.com/api/copilotkit \
  -t aleph-web:prod .
docker run -p 5173:5173 aleph-web:prod
```

Get it wrong and nothing raises: the image builds, the page loads, and every
request goes to `http://localhost:8000`, which on a user's machine is nothing.

### Tracing needs an object store

`--profile tracing` starts Langfuse, ClickHouse, an OTel collector **and MinIO**,
because Langfuse 3 ingests asynchronously through an object store and has no
filesystem mode. Two one-shots run first: `langfuse-db` creates the `langfuse`
Postgres database (Postgres' entrypoint only ever creates `POSTGRES_DB`, and
Langfuse's migrations assume the database exists), and `minio-buckets` creates
the buckets, because MinIO does not create them on demand and the first write to
a missing bucket is a `NoSuchBucket` at request time, long after everything
reported healthy.

Both are one-shots rather than `/docker-entrypoint-initdb.d` scripts on purpose:
initdb runs only on a first-boot empty data directory, so an initdb hook fixes a
fresh volume and leaves every existing stack unable to turn tracing on.

## Gates

CI (`.github/workflows/ci.yml`) runs four jobs, and each one can genuinely fail:

| job | what it runs |
|---|---|
| `python-quality` | `ruff check` · `ruff format --check` · `pyright` (strict, 0 errors) · `check-catalog-generated.sh` · `check-graph-state-keys.sh` |
| `python-unit` | `pytest -m "not integration"` |
| `python-integration` | postgres + redis services · `alembic upgrade head` · `alembic check` · `pytest -m integration` |
| `web` | `pnpm lint` · `pnpm build` · `npm ci` + `tsc --noEmit` for `apps/copilot-runtime` |

Run them locally with the commands in [`CLAUDE.md`](../CLAUDE.md#commands).

### The sweeps

A **sweep** is `scripts/check-*.sh`: a shell check that asserts one invariant across the whole tree,
for invariants a unit test cannot reach because they are about *absence* — a producer with no
consumer, a route with no scope check, a generated file nobody regenerated. There are 21, every one
of them is wired into `ci.yml`, and `check-sweeps-are-wired.sh` fails the build if a 22nd is added
without a consumer.

They are held to one rule rather than grandfathered past it: **a sweep must have a concrete failing
input.** `./scripts/acceptance.sh --self-check` proves it, mutating the tree so each sweep is made to
fail and restoring it afterwards — a check nobody has seen fail is an assumption wearing a green
light.

*Generated artifacts must match their source*

- `check-catalog-generated.sh` — `apps/web/src/a2ui/catalog.ts` and
  `apps/copilot-runtime/src/catalog.generated.ts` must match what `scripts/gen_catalog.py` renders
  from `catalog.json`. Fails on a hand-edit to a generated file, or on a `catalog.json` change
  committed without re-running the generator.
- `check-single-catalog.sh` — one catalog per component id, not one catalog per copy.
- `check-lint-count.sh` — the number of wiki lint checks the docs claim is the number `lint.py` runs.

*A write with no reader is the dominant defect class here*

- `check-graph-state-keys.sh` — every key a LangGraph node writes is declared on its state
  `TypedDict`; undeclared writes are discarded silently. It imports the analyzer from
  `tests/unit/test_graph_state_keys.py` rather than copying it, so the sweep and the behavioural
  tests cannot disagree.
- `check-surface-bindings.sh` — every surface prop a Python producer binds is declared in the
  client's zod schema. The binder resolves only declared props, so an undeclared one is dropped in
  silence: correct SSE payload, `undefined` at the view, nothing raised.
- `check-pane-registry.sh` — the client does not know what surfaces exist; the server names them.
- `check-agent-catalog-covers-renderer.sh` — the agent is not shown a shorter component list than
  the browser can draw.
- `check-web-dead-code.sh` — a React module nothing imports still type-checks and still lints.
- `check-web-dead-css.sh` — a CSS class nothing applies is invisible to every other gate here.
- `check-confidence-vocabulary.sh` — a claim's confidence was spelled four ways that disagreed.

*Security and scope*

- `check-project-scope.sh` — a route whose URL names a project resolves that project's scope.
- `check-agent-fs-permissions.sh` — the agent may read its standing orders and may not rewrite them.
- `check-agent-middleware.sh` — a tool that throws must not kill the conversation, everywhere rather
  than in the one place a test covers.
- `check-page-lock.sh` — a wiki page read without a row lock is a lost commit waiting to happen.
- `check-runtime-bridge.sh` — the Node bridge on `:4000` is not reachable from the whole network and
  does not forward unauthenticated.
- `check-compose-hardening.sh` — parses `docker compose config` (the merged, interpolated result the
  daemon is actually given, not the source text) and asserts: every long-running service restarts
  itself and every one-shot does not; every service bounds its logs; the five capped services cap
  swap too; a shell `LITELLM_BASE_URL` override actually reaches `api` and `workers`; and no image
  runs as root. It renders in a scratch directory with `.env.example` copied to `.env`, so it is
  hermetic and additionally proves the shipped example is sufficient to render the stack. Exit 0
  pass · 1 fail · **2 could not run** (no docker) — "could not measure" is never reported as "fine".

  **It is red today, on the last section only.** Four of the five Dockerfiles this stack builds
  declare no `USER`: `apps/api`, `apps/workers`, `apps/copilot-runtime` and `apps/web/Dockerfile.dev`
  run as uid 0. `apps/code-runner` and the new `apps/web/Dockerfile` do not. Fixing `api`/`workers`
  needs the `assets:` mount point chown'd at build time as well as the `USER` line — the named volume
  takes its ownership from the image the first time it is created, so adding `USER` alone breaks the
  asset store instead of securing it.

*The documentation is part of the system*

- `check-dead-refs.sh` — every path this repository names in prose resolves, and every sweep that
  exists is named in this file. A sweep nobody documents is a sweep nobody runs deliberately.
- `check-acceptance-claims.sh` — a ✅ row in `acceptance.md` names a test that exists and can be
  collected.
- `check-web-drift.sh` — the interface has a written specification and most of the app predates it;
  this measures the gap rather than asserting it is zero.
- `check-sweeps-are-wired.sh` — every `scripts/check-*.sh` is run by `ci.yml`, `acceptance.sh` or
  `self_check.sh`. An unwired sweep is a file, not a gate.
- `check-imports-resolve.sh` — every module a tracked file imports is itself tracked. `main.py` was
  committed importing two route modules that were never `git add`ed, so a clean checkout raised
  `ImportError` inside `create_app()` and the API could not start — while every local gate stayed
  green, because pytest, ruff and pyright all read the untracked files sitting in the working tree.
  Nothing else looks at the repository as somebody else would receive it.
- `check-migration-roundtrip.sh` — every migration's downgrade actually runs, not merely exists.
- `check-security-overrides.sh` — every entry in `pnpm-workspace.yaml`'s `overrides:` block names a
  package the lockfile still resolves. An override forces a transitive dependency to a patched
  version, and that claim expires: upstream moves, the direct dependency bumps its range, and the
  pin becomes something nobody needs that holds the tree back and reads to the next person as a live
  advisory. A stale one and a load-bearing one are identical on the page. It found `prismjs` on its
  first run — overridden for GHSA-x7hr-w5r2-h6wg, and nothing in the tree depends on it any more.

### On what is deliberately absent

There is **no eval gate**. There was one; it graded a fixture against itself — scorers read both
`expected` and `actual` out of the same JSONL line, so the system under test was never invoked and the
job was green regardless of the code. The datasets and the gate have been deleted. The scorers in
`packages/aleph-evals/src/aleph_evals/scorers/` are kept because the metrics themselves are correct;
they need a harness that actually calls Aleph plus a real dataset before they mean anything.

`aleph_evals.retrieval_eval` is now that harness for retrieval specifically — it calls
`search_corpus`, the production path, and reports recall@k and nDCG@10 as numbers. It is not a CI
gate because it needs a live gateway with an embedder; run it by hand and record the number.
## Observability

OTEL spans are exported through `otel-collector`; traces land in Langfuse (`:3000`). Spans follow
`<subsystem>.<op>`. LLM calls carry a `purpose` and are linked to `ModelCall` + `CostLedgerEvent` rows.

## Auth in deployment

Local mode is the compose default and skips user JWT verification entirely. Deploying means flipping
(plus `VITE_AUTH_MODE` for the frontend). Do not deploy without also closing the agent-endpoint gap
recorded in [`../CLAUDE.md`](../CLAUDE.md#known-broken--do-not-trust-these).
