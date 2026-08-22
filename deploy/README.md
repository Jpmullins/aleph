# Running Aleph

```bash
cp deploy/compose/.env.example deploy/compose/.env
# edit one line: where your models are
docker compose -f deploy/compose/docker-compose.yml up -d --wait
```

Then open <http://localhost:5173>.

`--wait` returns when the stack can actually serve, not when the processes have
started. Every service declares a readiness healthcheck, and the API's calls
`/readyz`, which round-trips the database, the queue and the asset store.

Every long-running service also carries `restart: unless-stopped`, so a crash or
a host reboot brings it back on its own. The one exception is a container the
operator stopped or killed by hand — `unless-stopped` honours that deliberately,
so `docker kill aleph-api-1` leaves it stopped and `docker compose up -d api`
is how you bring it back. See [`../docs/operations.md`](../docs/operations.md)
for the measured behaviour, the memory caps, and log rotation.

## Upgrading to non-root images

`apps/api`, `apps/workers`, `apps/copilot-runtime` and `apps/web/Dockerfile.dev`
now run as an unprivileged uid. Four of six images ran as root, which
`scripts/check-compose-hardening.sh` reports and CI enforces.

**One-time step on an existing deployment.** The `assets` named volume was
created while the container ran as root, so it is owned by uid 0 — verified:
`drwxr-xr-x 0 0`. Docker seeds a NEW volume's ownership from the image, so a
fresh stack is fine; an existing one is not, and the API cannot write an asset
until you chown it:

```bash
docker compose -f deploy/compose/docker-compose.yml down
docker run --rm -v aleph_assets:/mnt alpine chown -R 10001:10001 /mnt
docker compose -f deploy/compose/docker-compose.yml up -d
```

Skipping it does not fail silently: the asset-store capability probe writes and
reads a real object at boot, so the API refuses to come up and says which
capability could not answer. That is the intended behaviour — a stack that
starts and cannot store anything is worse than one that refuses to start.


## Aleph serves no models

There is no inference server in this stack and there should never be one. Aleph
connects **out** to any OpenAI-compatible endpoint — LiteLLM, Ollama, vLLM,
OpenRouter, a hosted API, or Bedrock behind a proxy. You point it somewhere;
it discovers what that endpoint offers and configures itself.

The one line you must set:

```
LITELLM_BASE_URL=http://host.docker.internal:4000
```

`host.docker.internal` means *your machine, from inside the container*. It
resolves natively on macOS and Windows, and `extra_hosts: host-gateway` in the
compose file makes the identical string work on Linux — so the same `.env` is
correct on every machine you run this on. Pointing at a remote endpoint is just
a different URL.

| your setup | value |
|---|---|
| LiteLLM proxy on this machine | `http://host.docker.internal:4000` |
| Ollama on this machine | `http://host.docker.internal:11434/v1` |
| vLLM on this machine | `http://host.docker.internal:8000/v1` |
| somewhere else | `https://models.example.com/v1` |

**Readiness does not depend on the model endpoint.** The stack comes up whether
or not your models are reachable, and reports the endpoint's status separately.
Gating boot on it would mean a typo in one URL looks identical to a broken
database.

```bash
LITELLM_BASE_URL=http://127.0.0.1:1 \
  docker compose -f deploy/compose/docker-compose.yml up -d --wait   # exits 0
curl -sf localhost:8000/readyz | jq .checks.litellm_gateway
# { "ok": false, "checked_age_s": 12.5, "max_age_s": 30.0,
#   "last_success_age_s": null, "stale": true, "in_verdict": false }
```

The gateway leg publishes the age of its own answer, so a cached "fine" cannot
outlive the endpoint: `ok` goes false within `max_age_s` of the gateway dying.
`/readyz?strict=1` puts the gateway back in the verdict and returns 503 — use it
to ask "can this answer a question", never as a container healthcheck.

## What runs by default

| service | port | what it is |
|---|---|---|
| `web` | 5173 | the workbench (the DEV image — see *Serving the built UI* below) |
| `api` | 8000 | HTTP + SSE, hosts the agent, owns migrations |
| `copilot-runtime` | 4000 | Node bridge between the agent and the browser |
| `workers` | — | ingest, research, reviewers |
| `code-runner` | — | sandboxed executor for agent-written code |
| `postgres` | 5432 | with pgvector |
| `redis` | 6379 | queues and streams |
| `migrate` | — | one-shot; everything else waits for it to exit 0 |

Nothing runs against a half-migrated schema: `migrate` is a one-shot service and
both apps declare `service_completed_successfully` on it.

## Optional profiles

```bash
docker compose -f deploy/compose/docker-compose.yml --profile tracing up -d --wait
```

- **`tracing`** — Langfuse (:3000), ClickHouse, an OTel collector, **and MinIO**:
  Langfuse 3 ingests through an object store and has no filesystem mode. Two
  one-shots run first — `langfuse-db` creates the `langfuse` Postgres database
  and `minio-buckets` creates the buckets, because nothing else does and the
  failure otherwise arrives as a crash loop, or — for Langfuse — a `NoSuchBucket`
  at request time. Aleph's own S3 asset store is not in that group: it calls
  `bucket_exists`/`make_bucket` on construction and always has.
  Off by default because it roughly doubles the stack for something you only need
  when you are debugging agent behaviour.
- **`s3`** — MinIO, for when you want assets in an object store rather than on
  the local filesystem. Set `ALEPH_ASSET_BACKEND=s3` as well.

## Serving the built UI

The stack runs `apps/web/Dockerfile.dev`: a Vite dev server with the source
bind-mounted, so an edit on your machine is live in the browser.
`apps/web/Dockerfile` is the production image — a compiled bundle behind nginx,
no Node at runtime, uid 101.

`import.meta.env.VITE_*` is substituted at **build** time, so the API URL is
baked into the bundle. Setting it in the container environment does nothing.

```bash
docker build -f apps/web/Dockerfile \
  --build-arg VITE_API_BASE_URL=https://aleph.example.com \
  --build-arg VITE_COPILOT_RUNTIME_URL=https://aleph.example.com/api/copilotkit \
  -t aleph-web:prod .
```

## How code-runner is contained

Agent-written code runs only here, and this is the only part of the stack where
containment is load-bearing rather than hygiene:

- **Its own `internal` network.** No route off the host at all. It can reach the
  queue and nothing else — not the API, not the database, not your model
  endpoint, not the internet.
- **No credentials.** It is deliberately not given `.env`.
- **`read_only` rootfs** with a `noexec` tmpfs for scratch.
- **`cap_drop: ALL`**, `no-new-privileges`, and a pid cap.
- **`mem_limit` = `memswap_limit`**, which is the only way to say "no swap".
  `deploy.resources` cannot express it, and removing the swap cap once let a
  runaway render take the host down.

## Editing code while it runs

```bash
docker compose -f deploy/compose/docker-compose.yml up --watch
```

`develop.watch` syncs source into the running containers and restarts what needs
restarting — a dependency change rebuilds, a source change does not.

## When something is wrong

```bash
docker compose -f deploy/compose/docker-compose.yml ps          # who is unhealthy
docker compose -f deploy/compose/docker-compose.yml logs -f api
curl -s localhost:8000/readyz | jq                              # which dependency failed
./scripts/check-compose-hardening.sh                            # is this file still sane
```

Two failure shapes worth recognising, because both were live in this file and
both look like something else:

- **`Up N seconds (health: starting)`, forever.** A memory cap below the working
  set: the kernel kills the container, the restart policy restarts it, and the
  loop reads as a slow boot. `docker inspect <c> --format '{{.State.OOMKilled}}'`.
- **`unhealthy` on a service that is answering fine.** A healthcheck that cannot
  pass — a probe naming a binary the image does not contain, or `localhost`
  resolving to an address family the server did not bind. Run the healthcheck's
  own command with `docker exec` and read the error rather than the verdict.

`/readyz` names the failing dependency rather than returning a bare 503, so the
answer to "why won't it start" is usually one command.

## Backing up, and getting it back

All durable state is in two named volumes — `postgres-data` and `assets` — and
neither is backed up by anything in this file. Nothing here runs on a schedule;
these are commands an operator runs.

```bash
./scripts/backup.sh                        # -> data/backups/aleph-<utc timestamp>/
./scripts/restore.sh <backup-dir> --into aleph_scratch
```

A backup nobody has restored from is a file, not a backup, so `restore.sh` does
not just run `pg_restore` — it compares the result against a manifest the backup
took **inside the dump's own snapshot**: every table's exact row count, every
table's content digest, every pgvector embedding component by component, the
extension versions, the alembic revision, and every trigger. Then it fires a
real `UPDATE` and a real `DELETE` at each append-only table and requires both to
be refused. It exits 1 and names the table if anything differs.

Both scripts take `--dry-run`. Full procedure, the traps, and the drill:
[`../docs/operations.md`](../docs/operations.md#backup-and-restore).

### Proving it works, without touching this stack

The drill runs against a **separate compose project**, so nothing here is
stopped or dropped:

```bash
./scripts/backup.sh --out /tmp/drill

export POSTGRES_PORT=5443 API_PORT_HOST=8010 REDIS_PORT=6390        RUNTIME_PORT=4010 WEB_PORT=5183
DC="docker compose -p alephdrill -f deploy/compose/docker-compose.yml"

$DC up -d postgres --wait                                  # an empty stack
./scripts/restore.sh /tmp/drill --into aleph --drop-existing   --cluster-url "postgresql://aleph:$POSTGRES_PASSWORD@localhost:5443/postgres"   --assets-volume alephdrill_assets
$DC up -d api --wait                                       # boots on the restored data
curl -s localhost:8010/readyz | jq .status                 # "ready"
curl -s localhost:8010/v1/projects | jq 'length'

$DC down -v                                                # drops ONLY the drill
```

Measured on this stack: 65 tables, 88,504 rows, 3,451 embeddings and 98 assets
restored into an empty stack, the API healthy against it, and the append-only
triggers still refusing an `UPDATE`. `-p alephdrill` is what makes the last line
safe — without it `down -v` destroys the volumes you just backed up from.

There is a scripted version of the database half:

```bash
uv run python scripts/_acceptance/restore_drill.py
```

It backs up the live database, restores into `aleph_restore_drill_<pid>`, re-derives
the ledger hash chain with the production verifier on both databases, and drops
the scratch database on the way out — including after a failure.

## Starting over

```bash
docker compose -f deploy/compose/docker-compose.yml down -v
```

`-v` drops the volumes, so this deletes every project, source and claim. **Take a
backup first** (above) — `docker volume rm` has no undo and neither does this.
