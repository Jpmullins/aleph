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

## What runs by default

| service | port | what it is |
|---|---|---|
| `web` | 5173 | the workbench |
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

- **`tracing`** — Langfuse (:3000), ClickHouse, and an OTel collector. Off by
  default because it roughly doubles the stack for something you only need when
  you are debugging agent behaviour. The tracing env vars stay set with the
  profile off; the exporters simply fail to deliver.
- **`s3`** — MinIO, for when you want assets in an object store rather than on
  the local filesystem. Set `ALEPH_ASSET_BACKEND=s3` as well.

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
```

`/readyz` names the failing dependency rather than returning a bare 503, so the
answer to "why won't it start" is usually one command.

## Starting over

```bash
docker compose -f deploy/compose/docker-compose.yml down -v
```

`-v` drops the volumes, so this deletes every project, source and claim. There
is no undo.
