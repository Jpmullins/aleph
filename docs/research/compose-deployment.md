# Docker Compose deployment for a system that serves no models

Research date: **19 August 2026**. Everything below was checked against live sources on that date;
where I could not verify something I say so.

---

## In one paragraph

Docker Compose is not where you left it. The tool is now at **v5.5.0** (17 Aug 2026) — it skipped v3
and v4 to get away from the old "compose file format version" numbers — and the two changes that
matter to Aleph are that **builds are now delegated to Docker Bake** instead of Compose's own
builder, and that **Compose grew real init containers** (`pre_start`, v5.3.0, July 2026), which is
the correct home for database migrations. The single hardest problem in Aleph's deployment is not
Compose at all: it is that Aleph's *model endpoint lives outside the stack* and may be on the host,
on another machine, or in the cloud, and the operator must not have to edit a file to say which. The
answer is one line of Compose (`extra_hosts: ["host.docker.internal:host-gateway"]`) applied to every
service that talks to models, plus one environment variable holding a plain URL — that combination
covers macOS, Linux, and remote endpoints identically. Everything else is hygiene: put optional
subsystems (tracing, object store, a bundled local model server) behind **profiles** so the default
stack is small; give every stateful service a healthcheck that proves *readiness*, not *liveness*, so
`depends_on: condition: service_healthy` is telling the truth; keep provider API keys out of image
layers and out of `docker inspect` by supporting a `_FILE` suffix on every secret; and invest real
effort in the first-run path, because the failure the operator will actually hit is "my model
endpoint is unreachable" and a generic stack trace is a support ticket.

---

## Vocabulary (so nothing below is guesswork)

| Term | What it means here |
|---|---|
| **Compose Specification** | The YAML schema. Community-owned, versionless. Distinct from the CLI version. |
| **Compose CLI** | The `docker compose` binary/plugin. Currently v5.x. |
| **Service** | One container definition in `compose.yaml`. |
| **Profile** | A label on a service. Services with a profile only start when that profile is enabled. |
| **Healthcheck** | A command Compose runs inside a container periodically; exit 0 = healthy. |
| **Liveness** | "The process has not died." |
| **Readiness** | "The process can actually serve a real request right now" — includes its dependencies. |
| **Init container** | A short-lived container that runs to completion *before* the real one starts. |
| **Bind mount** | A host directory mapped into a container. Changes on either side are visible on both. |
| **Named volume** | Storage Docker manages by name. Not a host path you edit by hand. |
| **`host-gateway`** | A magic value Docker replaces with the IP of the host, as seen from a container. |
| **OTLP** | OpenTelemetry Protocol — the wire format for traces. Has an HTTP flavour and a gRPC flavour. |

---

## 1. What is actually current (August 2026)

I verified these against the GitHub releases API, official docs, and vendor release notes.

| Thing | Version | Date | Status |
|---|---|---|---|
| Docker Compose CLI | **v5.5.0** | 17 Aug 2026 | current |
| Compose v5.0.0 "Mont Blanc" | v5.0.0 | 2 Dec 2025 | the break: internal builder removed |
| Docker Engine | **29.7.2** | 5 Aug 2026 | current |
| PostgreSQL | **18.6** | 13 Aug 2026 | current stable (18.5 was never shipped — regression) |
| PostgreSQL 19 | 19 Beta 3 | 13 Aug 2026 | beta; GA ~Sept/Oct 2026 |
| pgvector | **0.8.6** | 29 Jul 2026 | current; image tag `0.8.6-pg18-trixie` |
| Redis | 8.x | — | AGPLv3 (tri-licensed) since Redis 8 |
| Valkey | **9.1** | May 2026 | BSD-3, Linux Foundation, wire-compatible fork |
| ClickHouse | 26.7.x current; **25.8** and **26.3** are LTS | Aug 2026 | Langfuse v4 needs ≥25.12 |
| Langfuse | **v4** | 2026 | ClickHouse acquired Langfuse Jan 2026; core stays MIT |
| OTel Collector (contrib) | **0.158.0** | 4 Aug 2026 | current |
| MinIO (community) | — | repo archived **12 Feb 2026** | **dead** — see §5 |
| LiteLLM proxy | ~v1.85 | 2026 | still the common gateway; not the only one |

### Compose v5 — the four changes that touch Aleph

1. **v5.0.0 (Dec 2025): the internal builder was removed.** `docker compose build` now delegates to
   **Docker Bake**, the same engine `docker build` uses. Upside: better caching, multi-platform
   builds, real BuildKit features. Downside: there is a live tail of bug reports around
   `COMPOSE_BAKE` and `.env` resolution (Compose issues #12989, #13124, #12774 — builds not picking
   up env vars, and containers not being recreated with a freshly built image). **Do not set
   `COMPOSE_BAKE` yourself**; take the default, and if a rebuild seems not to take effect, that
   family of bugs is the first suspect.
2. **v5.3.0 (2 Jul 2026): init containers**, via a `pre_start` list on a service. Each step runs in
   its own throwaway container, in order, and the service only starts when every step exits 0. This
   replaces the `depends_on: {condition: service_completed_successfully}` one-shot pattern — with a
   caveat that is load-bearing for migrations (§6).
3. **v5.4.0 (3 Aug 2026): resource reconciliation** for volumes and networks — Compose is better at
   noticing when a declared volume/network no longer matches reality and recreating it.
4. **v5.5.0 (17 Aug 2026): image digest reconciliation** overhauled to stop needless container
   recreation, and `compose pull` now honours `pull_policy` refresh windows (`daily`, `weekly`,
   `every_N`). Note the release itself warns that **existing containers may be recreated on the first
   `compose up` after upgrading**.

### A source conflict, stated honestly

GitHub's own changelog (30 Jan 2026) says hosted runners were moving to "Docker Compose v2.40" in
February 2026, while the docker/compose releases API returns v5.x for the same period and the Docker
docs repo vendors its CLI reference from a path containing `compose/v5/`. I trust the releases API
and the docs vendor path; the GitHub Actions note most likely refers to a distribution channel that
lags. **Practical consequence:** do not assume the operator's Compose is v5. Aleph should print its
minimum required version and check it at first run (§11), because a v2-era Compose meeting a
`pre_start` key produces a schema error, not a graceful skip.

### What is obsolete, and should not appear in the new `deploy/`

- **`version:` at the top of the file.** Ignored since Compose v2 (2022); emits
  `the attribute 'version' is obsolete, it will be ignored`. Delete it.
- **`links:`.** Superseded by networks + service-name DNS. Never needed.
- **The filename `docker-compose.yml`.** Compose searches `compose.yml`, `compose.yaml`,
  `docker-compose.yml`, `docker-compose.yaml` in that order. `compose.yaml` is the current
  convention. (Aleph's old file used `docker-compose.yml`.)
- **`provider:` services** for AI models. Superseded by the top-level `models:` element. Aleph should
  use *neither* — see §3.
- **The idea that `deploy:` requires Swarm.** False for the `resources` subsection: Compose v2+
  applies `deploy.resources.limits` on a plain `docker compose up`.

---

## 2. The shape I would build

```
deploy/
  compose.yaml              # the whole stack. Production-shaped. Everything pinned.
  compose.override.yaml     # auto-loaded dev ergonomics: watch, source mounts, exposed ports
  .env.example              # non-secret defaults + documented knobs
  .env                      # gitignored, 0600
  secrets/                  # gitignored; generated on first run
    .gitkeep
  init/
    postgres/00-aux-db.sh   # first-init only (see §6)
  otel/collector.yaml
  README.md
aleph                       # one entrypoint script: up / down / doctor / logs / backup
```

**One file plus profiles, not a fan of override files per environment.** Aleph is a
single-operator, self-hosted system: there is exactly one deployment, the operator's machine. The
real axis is not dev-vs-prod, it is *"I am hacking on Aleph"* vs *"I am running Aleph"*, plus
*"which optional subsystems do I want today."* That maps cleanly onto:

- **profiles** for optional *capability* — tracing, object store, a bundled model server, GPU,
  admin tools;
- **exactly one `compose.override.yaml`** for the hacking mode — source mounts, `develop.watch`,
  dev-only ports.

The trap to document loudly: **`compose.override.yaml` is auto-loaded when present.** Someone
running Aleph for real must use `docker compose -f compose.yaml up` (or set
`COMPOSE_FILE=compose.yaml`), or they silently get the dev config. Put a banner comment at the top of
the override, and make the `aleph` wrapper script pass `-f` explicitly in run mode. Have the doctor
print which files were merged — `docker compose config` shows the final merged YAML and is the
single best debugging command in the whole system.

Profiles I would define:

| Profile | Contains | Default? |
|---|---|---|
| *(none)* | postgres, redis/valkey, api, workers, web, runtime, code-runner + its redis | **on** |
| `tracing` | langfuse-web, langfuse-worker, clickhouse, langfuse-cache, langfuse-blob, otel-collector | off |
| `s3` | object store + bucket init | off |
| `local-llm` | LiteLLM / Ollama / vLLM, for operators who want one in-project | off |
| `gpu` | GPU-enabled variant of the above | off |
| `tools` | psql shell, backup job, one-off scripts | off (target directly) |

One profile subtlety worth knowing: if you name a profiled service directly on the command line
(`docker compose run backup`), Compose runs it *without* enabling its profile — but it will only pull
in that service's own `depends_on`. If a profiled service depends on another profiled service, you
must enable the profile. Keep `tools` services depending only on always-on services.

---

## 3. The crux — reaching models that live outside the stack

This is the part to get right. Aleph serves no models, so **every deployment has an external
dependency Compose does not manage**, and it can be in one of four places:

| Where the endpoint is | What the operator sets |
|---|---|
| On the host machine (Ollama, LM Studio, a locally-run LiteLLM) | `http://host.docker.internal:11434/v1` |
| Another machine on the LAN | `http://192.168.1.50:4000/v1` |
| In the cloud | `https://gateway.example.com/v1` |
| In the same Compose project (`local-llm` profile) | `http://litellm:4000/v1` |

### The one line that makes all four work

```yaml
x-model-egress: &model-egress
  extra_hosts:
    - "host.docker.internal:host-gateway"
  environment:
    ALEPH_MODEL_ENDPOINT_URL: ${ALEPH_MODEL_ENDPOINT_URL:?set this in deploy/.env — see README §Models}
```

Apply the anchor to every service that talks to models (api, workers, and any preflight job).

Why this works everywhere:

- On **Docker Desktop (macOS/Windows)** `host.docker.internal` already resolves. Declaring it again
  via `extra_hosts` is redundant but harmless — no conflict.
- On **Linux** there is no auto-mapping and there never will be, because Docker runs natively there;
  `host-gateway` is the supported opt-in. It has worked since Docker 20.10 (2020) and works on every
  version since.
- For a **remote** endpoint nothing special happens: it is a plain URL and normal container egress.
- For an **in-project** endpoint it is a service name on the Compose network.

So the Compose file never encodes *which* case it is. The operator changes one string.

### The Linux gotchas that will actually bite people

These are the real failure modes, in the order they occur:

1. **The model server is bound to loopback.** Ollama listens on `127.0.0.1:11434` by default, so
   even with correct container-side networking, nothing answers. Fix: `OLLAMA_HOST=0.0.0.0` (and
   restart Ollama); vLLM needs `--host 0.0.0.0`; LiteLLM needs `--host 0.0.0.0`.
2. **The host firewall drops traffic from the Docker bridge.** `ufw`/`firewalld` on Linux will
   silently drop packets arriving from `172.17.0.0/16`. This looks identical to "server not running."
   The doctor must distinguish them (§11).
3. **Docker Engine 29 makes nftables available** as an opt-in firewall backend
   (`firewall-backend: nftables`). If an operator has switched, their existing iptables allow-rules
   for the Docker bridge do not apply. Worth one line in the troubleshooting doc.
4. **Rootless Docker and Podman-compat** put the host at a different address. Document
   `ALEPH_MODEL_ENDPOINT_URL` with a raw IP as the always-works fallback.

### What not to do

- **Do not use `network_mode: host`.** It is Linux-only in any useful form (Docker Desktop's host
  networking on macOS is a limited feature, not a peer of the Linux behaviour), it destroys
  service-name DNS between Aleph's own containers, and it removes port isolation. `hermes-agent` in
  `~/Documents/code/inspiration/` uses `network_mode: host` for its whole stack — that is a
  single-container app where the trade is cheap, and it is the wrong model for a seven-service stack.
  Keep it documented as an escape hatch only.
- **Do not add a bundled model runner to the default stack.** Compose has a top-level `models:`
  element now (Docker Model Runner) that pulls models as OCI artifacts and injects connection details
  into services. It is genuinely neat and it is **exactly the thing Aleph's constraints forbid** —
  using it would make Aleph a model server. Mention it in docs as "we deliberately do not use this."
- **Do not name the variables after a vendor.** Today's code uses `LITELLM_BASE_URL` and
  `INSIGHTS_LITELLM_API_KEY`. The 2026 gateway landscape is not one product — LiteLLM (Python),
  Bifrost (Go, claims ~50x lower P99 overhead at 5k rps), Kong AI Gateway, Portkey, plus plain
  vLLM/Ollama, all speaking the same OpenAI-compatible shape. Rename to
  `ALEPH_MODEL_ENDPOINT_URL` / `ALEPH_MODEL_ENDPOINT_KEY` and keep the old names as deprecated
  aliases for one release. The endpoint being interchangeable is the whole point of the constraint.

---

## 4. Secrets

### The three places a provider key leaks

1. **Into an image layer** — `ARG KEY` + `ENV KEY`, or `COPY .env .`. Permanent, shipped to whoever
   pulls the image. Never do this. If a build genuinely needs a credential, BuildKit's
   `--mount=type=secret` exists and does not persist into layers.
2. **Into `docker inspect`** — anything under a service's `environment:` is visible to anyone in the
   `docker` group and lands verbatim in any bug report where someone pastes `docker inspect` output.
   This is the leak people forget.
3. **Into the repo** — a committed `.env`. Prevented by `.gitignore` and by never shipping a `.env`
   with real values, only `.env.example`.

### What Compose offers

Compose `secrets` come in two source flavours:

```yaml
secrets:
  model_endpoint_key:
    file: ./secrets/model_endpoint_key      # read from a file on disk
  langfuse_secret:
    environment: LANGFUSE_SECRET_KEY        # read from a host env var
```

Either way, the secret is mounted **read-only as a file at `/run/secrets/<name>`**, in memory, not
in the container's environment. `uid`/`gid`/`mode` in the long syntax only apply when the source is
`environment`.

The `environment:` source is the underrated one for a single-operator system: it lets the operator
keep keys in a password manager and run `op run -- docker compose up` or
`pass show aleph/key | ... docker compose up`, with nothing on disk at all, while the container still
sees a file rather than an env var.

### The pragmatic choice for Aleph

Be honest about the threat model. On a single-tenant self-hosted box, `.env` at mode 0600 outside the
image is *already* adequate against the realistic adversary. The concrete gain from `/run/secrets` is
narrow but real: it removes the `docker inspect` exposure, which is the one that leaks by accident.

So: **support both, with a file taking precedence.** Adopt the `_FILE` suffix convention that the
official Postgres, MySQL, and Nextcloud images all use:

```
ALEPH_MODEL_ENDPOINT_KEY_FILE=/run/secrets/model_endpoint_key   # wins if set and readable
ALEPH_MODEL_ENDPOINT_KEY=sk-...                                 # fallback, dev convenience
```

Implement it once in the settings layer (a small `resolve_secret(name)` that checks `<NAME>_FILE`
then `<NAME>`), apply it to every credential-bearing setting, and the Compose file can then offer
both spellings without the app knowing which one is in use. Default the shipped `compose.yaml` to
the `_FILE` form and let `compose.override.yaml` (dev) use plain env.

**Generate secrets on first run.** The old `.env.example` asked the operator to hand-run
`openssl rand -hex 32` six times, base64 a colon-joined pair by hand, and paste it all correctly.
That is a first-run failure generator. The `aleph up` wrapper should create
`deploy/secrets/*` from `openssl rand` when they do not exist, chmod 0600, and never overwrite. The
only value a human should have to supply is the model endpoint URL and its key.

---

## 5. The stateful services

### Postgres + pgvector

Use **`pgvector/pgvector:0.8.6-pg18-trixie`**. Pin the full tag, not the floating `pg18` — a floating
tag turns a `compose pull` into an unplanned extension upgrade.

- **Keep the `/var/lib/postgresql` mount** the old file used. PG18 wants the volume at the *parent*
  of the data directory so it can manage major-version subdirectories. Mounting the old
  `/var/lib/postgresql/data` path on PG18 is a known foot-gun.
- **Do not adopt PostgreSQL 19 yet.** It is at Beta 3 (13 Aug 2026), GA is expected Sept/Oct 2026,
  and pgvector will need a matching build. Revisit after GA plus one minor.
- **Named volume, not a bind mount**, for the data directory. Bind mounts inherit host uid/mode
  quirks that PG refuses to start on. Bind mounts are correct for *source* and for `data/assets`
  (where the host uid must match, which the old stack handled with `ALEPH_UID`/`ALEPH_GID` — keep
  that; it was right).
- **Backups are `pg_dump`, not volume copies.** A filesystem copy of a running PG data directory is
  not a consistent backup. Ship a `tools`-profile service:
  `pg_dump -Fc` on a schedule or on demand, writing to a `backups` volume, gated behind a
  `pg_isready` wait. Test restore, or you do not have backups.

### Queue: Redis or Valkey

Redis 8 is AGPLv3 (tri-licensed). For a self-hosted system that does not modify Redis and does not
offer Redis-as-a-service, AGPL changes nothing in practice — the copyleft trigger is modifying the
source and offering *that* over a network. But **Valkey 9 (BSD-3, Linux Foundation, 9.1 in May 2026)
removes the question entirely**, is wire-compatible, and is what most distros and AWS now default to.
Aleph uses Redis only as an `arq` job queue and a pub/sub bus — no Redis Stack modules, no JSON type,
no probabilistic structures. **Recommend Valkey**; it is a drop-in tag change with no client change.

Keep the old file's genuinely good decision: **three separate queues** — the platform queue, the
code-runner's isolated queue on an `internal: true` network, and (under the `tracing` profile)
Langfuse's own. That isolation is why a sandbox escape cannot reach agent tokens. Do not merge them
to save a container.

### Object store — the landscape changed under you

**MinIO community is gone.** Timeline: the admin console was gutted in May 2025; Docker images and
prebuilt binaries for the community edition stopped in October 2025; the repository was marked "no
longer maintained" and archived on **12 February 2026**. Aleph's old compose pins
`minio/minio:RELEASE.2025-09-07T16-13-09Z` in three places — that tag still pulls today but nothing
behind it is maintained, and it will not receive CVE fixes.

Options, in order of how much I would trust them for this use:

1. **Do not run one by default.** Aleph's asset backend already defaults to the local filesystem.
   This is the correct answer and it was already the design. Keep object storage behind the `s3`
   profile and treat it as "for testing the S3 code path, or for pointing at a real external S3."
2. **`cgr.dev/chainguard/minio`** — Chainguard's rebuilt MinIO on their free Starter tier, rebuilt
   daily from source, CVE-patched. **This is what Langfuse's own `docker-compose.yml` now uses**,
   which is the strongest available signal that it is the pragmatic successor. Caveat: Chainguard's
   free tier historically carries only the latest tag, so version-pinning is limited.
3. **Garage** (lightweight, geo-distributed, used by Mastodon/Matrix/PeerTube) or **SeaweedFS** if
   you want a maintained upstream rather than a rebuild of an abandoned one. **RustFS** is also
   frequently listed; I did not verify its maturity and would not adopt it sight-unseen.

### Observability

Langfuse is now at **v4**; ClickHouse acquired Langfuse in January 2026 and the core stayed MIT and
self-hostable. Architecture is unchanged from v3 — web + worker containers, plus PostgreSQL,
**ClickHouse ≥ 25.12 (26.4 recommended)**, Redis/Valkey, and an S3-compatible blob store. Langfuse's
own compose currently pins `clickhouse-server:25.12`, `redis:7`, `postgres:17`, and
`cgr.dev/chainguard/minio`, with images `docker.langfuse.com/langfuse/langfuse:4` and
`langfuse-worker:4`.

That is **five extra containers and several gigabytes of RAM** for tracing. The old Aleph stack ran
all of it by default on a 16 GB laptop and had to hand-tune `mem_limit` on the Langfuse web container
to stop Node OOM-crash-looping. That is a strong argument for the `tracing` profile.

The collector question changed too: **Langfuse v4 ingests OTLP directly** at `/api/public/otel`, with
HTTP Basic auth (`base64(public_key:secret_key)`) and a header
`x-langfuse-ingestion-version=4`. It supports **OTLP over HTTP (JSON and protobuf) but not gRPC.**
Aleph currently exports OTLP/gRPC to a collector which translates. Two valid designs:

- **Drop the collector.** App exports OTLP/HTTP straight to Langfuse. One less container, one less
  config file. Costs you buffering on Langfuse restarts and any fan-out to a second backend.
- **Keep the collector** (`otel/opentelemetry-collector-contrib:0.158.0`, 4 Aug 2026) inside the
  `tracing` profile. Keeps gRPC, gives you a retry buffer and a place to add a second exporter later.

Either is defensible. What is **not** defensible is the current shape, where the default stack
requires the whole tracing subsystem to boot. Whichever you pick, make sure the app runs cleanly with
`OTEL_SDK_DISABLED=true` and **test that path**, because a configuration that only works with tracing
enabled is a configuration whose default is untested.

---

## 6. Migrations on startup — and the trap in the new feature

The question was: init container or entrypoint? The answer is **init container**, and never
entrypoint — but the specific mechanism needs care.

**Why not the entrypoint.** If the app's entrypoint runs `alembic upgrade head`, then the moment
there is more than one API process or the workers start concurrently, N processes race the same
migration. Alembic does not take a lock for you. It also couples "can I start" to "can I migrate",
which is exactly the coupling you want to break when debugging.

**What Compose 5.3 gives you.** `pre_start` is a list of init containers declared on the service
itself:

```yaml
services:
  api:
    image: aleph-api
    pre_start:
      - command: ["alembic", "upgrade", "head"]
        working_dir: /app/apps/api
    depends_on:
      postgres: { condition: service_healthy }
```

Supported attributes are `command`, `image`, `user`, `privileged`, `working_dir`, `environment`,
`per_replica`. The step joins the service's networks, so it can reach `depends_on` targets. Steps run
in declared order; a non-zero exit fails the bring-up of the service *and its dependents*.

**The trap, quoted from the reference:** *"A `pre_start` step that has already succeeded for its
current definition is not re-run on a subsequent `up`, nor when the service container restarts under
its `restart` policy. A step runs again when its definition changes, when the previous run did not
succeed, or when the service is recreated."*

For migrations this cuts both ways:

- **Image-based running** (the operator pulls or rebuilds the API image): a new image means the
  service is recreated, so `pre_start` re-runs. Correct behaviour, free.
- **Source-mounted dev** (code bind-mounted, image unchanged): you add a migration, run
  `docker compose up`, and **the migration does not run.** The service boots against an unmigrated
  schema and you get `relation "..." does not exist` — which is precisely the failure class the old
  one-shot `aleph-migrate` service was introduced to kill.

`pre_start` also does not work with anonymous volumes or tmpfs mounts, is not transactional (a failed
step does not roll back earlier ones), and by default does not run per-replica.

**Recommendation:** keep a one-shot `migrate` service with
`depends_on: {migrate: {condition: service_completed_successfully}}`. It re-runs unconditionally on
every `up`, works on Compose ≥2.17, and `alembic upgrade head` is a no-op at head so the cost is a
second. Note `pre_start` in the file as the tidier successor once dev stops bind-mounting source, or
use it *in addition* for things that genuinely should run once (fixing volume ownership, generating
a config). Do not spend the migration correctness guarantee on tidiness.

**Postgres `docker-entrypoint-initdb.d`** runs **only when the data directory is empty**. It is
correct for the one thing the old stack used it for — creating the auxiliary `langfuse` database
before the port opens — and useless for anything that must happen more than once. Keep it, scoped to
that, with a comment saying why. (It also has to be gated behind the `tracing` profile now, or the
default stack creates a database nothing uses; harmless, but confusing.)

---

## 7. Healthchecks that assert readiness

`depends_on: condition: service_healthy` is only as honest as the healthcheck under it. A liveness
check ("the process has a PID") makes the dependency ordering a lie: Compose says the DB is healthy,
the API connects, and the API dies.

Rules I would hold:

- **Every stateful service gets a readiness check that touches its real read path.** Not
  "port is open" — an actual query.
  - Postgres: `pg_isready -U $USER -d $DB` is the baseline, but `pg_isready` returns success while
    the aux init script is still running on first boot. If ordering matters, use
    `psql -c 'select 1' -d $DB`. (Aleph's kernel already has the right idea: a capability that
    cannot answer a live query must not come up. Same principle, one layer down.)
  - Redis/Valkey: `redis-cli ping` (with `-a` if a password is set).
  - ClickHouse: `wget --spider http://localhost:8123/ping`.
  - Aleph API: **`/readyz`, which already exists** and probes DB + Redis + asset store. Use it. Use
    `/healthz` for nothing that gates ordering.
- **Use `start_period` and `start_interval` together.** `start_period` is a grace window during which
  failures do not count against `retries`; `start_interval` (Engine 25+) is the faster poll cadence
  used *during* that window. `start_period: 60s` + `start_interval: 1s` + `interval: 30s` means a
  slow cold boot does not burn retries, a fast boot is detected in about a second, and a healthy
  steady state costs almost nothing.
- **Watch what the image actually ships.** `curl` is not in every image; Langfuse's Next.js standalone
  binds to the container's eth0 rather than loopback, which is why the old file's
  `wget --spider http://$(hostname):3000/...` was correct and a `localhost` probe was not. That
  comment in the old file is worth carrying forward verbatim — it encodes a debugging session.
- **Engine 29 surfaces health in `docker ps`** — `docker ps --format '{{.HealthStatus}}'` and a
  `Health` field on `GET /containers/json`. The doctor script should use that rather than parsing
  `docker inspect`.

### The one thing that should *not* be in `/readyz`

**Do not make the model endpoint a readiness dependency of the API.** If the operator's gateway is
down or misconfigured, the correct behaviour is: Aleph boots, the UI loads, and a prominent banner
says exactly what is wrong and how to fix it. Making it a readiness failure gives you a crash-looping
stack with the real message buried in `docker compose logs`, which is the worst possible version of
that experience. Model-endpoint status is a *first-class, visible application state*, not a health
gate.

---

## 8. Resource limits and restart policies

Two spellings exist and both work without Swarm:

```yaml
# spec-native
deploy:
  resources:
    limits: { memory: 2g, cpus: "2.0", pids: 256 }
    reservations: { memory: 512m }

# legacy top-level
mem_limit: 2g
memswap_limit: 2g
cpus: 2.0
```

Pick one and hold it — a file with both is unreadable. `deploy.resources` is the spec-native
recommendation.

**But there is a real reason the old file used the legacy spelling.** Setting
`memswap_limit == mem_limit` means *no swap for that container*: a runaway service gets OOM-killed
inside its own cgroup and restarted, instead of paging the whole host to death. The old file's header
comment records that exact incident (a "bootstrap research stampede" on 2026-06-11). The `deploy`
schema has no memswap equivalent. So:

- If the no-swap behaviour matters — and for an agent harness that can spawn unbounded work, it does
  — **keep `mem_limit` + `memswap_limit`** and write down why. This is a case where the
  "modern" spelling is strictly less expressive.
- Either way, keep the limits themselves. They are the difference between one bad job and a dead
  laptop.

Restart policies: `restart: unless-stopped` for long-lived services, `restart: "no"` for one-shots.
Be aware `unless-stopped` plus a service that dies during startup produces a silent restart loop —
the healthcheck plus the doctor script are what make that visible.

---

## 9. Developer experience

**Use `develop.watch`, not bind-mounted source, for the hot paths.** Compose watch syncs changed
files into the running container rather than mounting a host directory, which on macOS avoids the
filesystem-translation penalty entirely; reported trigger latency is under 500 ms.

```yaml
services:
  web:
    develop:
      watch:
        - action: sync
          path: ./apps/web/src
          target: /app/apps/web/src
          initial_sync: true
          ignore: ["node_modules/"]
        - action: rebuild
          path: ./apps/web/package.json
  api:
    develop:
      watch:
        - action: sync                     # uvicorn --reload picks it up
          path: ./apps/api/src
          target: /app/apps/api/src
          initial_sync: true
        - action: sync+restart             # config change, no rebuild needed
          path: ./deploy/otel/collector.yaml
          target: /etc/otel/config.yaml
        - action: rebuild                  # dependency change
          path: ./pyproject.toml
```

Reference notes worth knowing:

- Actions: `sync`, `rebuild`, `restart` (2.32+), `sync+restart` (2.23+), `sync+exec` (2.32+).
  `sync+exec` runs a command after syncing — the right tool for "reload without restarting."
- `target` is required for `sync` actions. `initial_sync: true` makes Compose reconcile existing
  containers at watch startup rather than waiting for the first change.
- `ignore` uses `.dockerignore` syntax and inherits `.dockerignore` patterns automatically;
  `include` is the inverse. **Quote glob patterns** — a leading `*` is special in YAML.
- Run with `docker compose up --watch` (or `docker compose watch`). Not every service needs it.

For Aleph specifically: `sync` + `uvicorn --reload` for the API beats `rebuild`, because a rebuild
through Bake on a `uv` workspace with 21 packages is not a two-second operation.

---

## 10. GPU passthrough (optional profile, brief)

Aleph does not need it. An operator running Ollama or vLLM inside the same Compose project does.

Two spellings, both current:

```yaml
services:
  vllm:
    profiles: ["gpu"]
    gpus: all                              # service-level shorthand
    # --- or, for more control ---
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["0"]            # count: and device_ids: are mutually exclusive
              capabilities: [gpu]          # REQUIRED — omitting it errors at deploy
```

Notes:

- `capabilities` is mandatory in the long form; leaving it out is a deploy-time error, not a warning.
- `count: all` or omitted = all host GPUs. `count` and `device_ids` cannot both be set.
- This requires the **NVIDIA Container Toolkit installed on the host**. Compose cannot check that for
  you; the doctor should, and should say so plainly.
- **Apple Silicon has no GPU passthrough into containers and will not.** On a Mac, the only way to
  use the GPU is to run Ollama/LM Studio *on the host* — which is precisely why the
  `host.docker.internal` path in §3 is not optional politeness, it is the primary path for a large
  share of operators.
- Ship this as `compose.gpu.yaml` or a `gpu` profile carrying *only* the model-server service. Aleph
  itself must never gain a GPU dependency.

---

## 11. First run, and designing the failure messages

The happy path is easy. The failures are the product.

### The sequence

```
git clone && cd aleph
./aleph up
```

`./aleph up` should, in order:

1. **Check tool versions.** `docker compose version` against a documented minimum; `docker info` to
   confirm the daemon is reachable. Fail here with a specific message, not later with a schema error.
2. **Create `deploy/.env` from `.env.example` if absent**, and **generate every random secret** into
   `deploy/secrets/` (0600). The operator supplies two values, not eight.
3. **Create bind-mount directories with the invoking uid** *before* `compose up` — otherwise the
   daemon creates them root-owned and the api/workers containers cannot write. (The old
   `bootstrap-local.sh` did this and the comment explaining why should survive.)
4. `docker compose up -d --wait --wait-timeout 180`. `--wait` blocks until every service with a
   healthcheck is healthy and exits non-zero if not. Services *without* a healthcheck are considered
   ready immediately — one more reason every service needs one.
5. **Run the doctor** and print it, whether or not the stack came up.
6. Print the URLs.

### Make Compose itself produce good errors

Use required-variable interpolation for anything with no sensible default:

```yaml
ALEPH_MODEL_ENDPOINT_URL: ${ALEPH_MODEL_ENDPOINT_URL:?not set. Copy deploy/.env.example to deploy/.env and set it to your OpenAI-compatible endpoint. See deploy/README.md#models}
```

Compose refuses to start and prints the message. Known rough edge: some Compose versions emit an
extra `invalid interpolation format` line alongside the real message (issue #10293), so the doctor
should still catch this case earlier and more prettily. `docker compose config` is the command that
proves interpolation resolved correctly, and belongs in the troubleshooting doc.

### The model-endpoint diagnosis, as a decision tree

This is the failure that will dominate support. Walk it in order and report the **first** step that
fails, with a cause and a fix — never a stack trace:

| Step | Failure message shape |
|---|---|
| 1. Parse URL | `ALEPH_MODEL_ENDPOINT_URL is "localhost:11434" — missing scheme. Use http://host.docker.internal:11434/v1` |
| 2. DNS resolve, **from inside a container** | `Cannot resolve "host.docker.internal" from inside the stack. On Linux this needs extra_hosts: ["host.docker.internal:host-gateway"] — it should already be in compose.yaml. Are you on an old Docker (<20.10)?` |
| 3. TCP connect | `Resolved host.docker.internal → 172.17.0.1 but nothing is listening on :11434.` Then the three ranked causes: **(a)** the server is not running; **(b)** it is bound to 127.0.0.1 — set `OLLAMA_HOST=0.0.0.0` / `--host 0.0.0.0` and restart; **(c)** a host firewall is dropping the Docker bridge — `sudo ufw allow from 172.17.0.0/16`. Include the exact commands. |
| 4. HTTP response | `Connected, but GET /v1/models returned 502. Something is listening on :11434 but it is not an OpenAI-compatible API. Is that the right port?` |
| 5. Auth | `GET /v1/models returned 401. ALEPH_MODEL_ENDPOINT_KEY is set (12 chars, starts "sk-abc…") but was rejected.` **Never print the key.** |
| 6. Non-empty list | `Endpoint reachable and authenticated, but /v1/models returned zero models. Your gateway is running with no models configured, or your virtual key is scoped to none.` |
| 7. Capability coverage | `Gateway serves 4 models. Aleph could not bind a model for capability "embedding": no listed model reports an embedding mode, and none responded to an embedding probe. Retrieval will not work. Models seen: a, b, c, d.` |
| 8. Probe | `Model "x" is advertised but returned "Model access is denied" when invoked. Skipping it.` |

Steps 6–8 already exist in Aleph as `scripts/verify-gateway.sh` and `aleph_models.discovery` — and
that script's header comment is the best piece of writing in the repo on this subject. It learned the
right lesson (list, then *probe*, because an advertised model is not a reachable one). Keep that
logic; move the transport into a container so it tests the path Aleph actually uses, and surface the
result in the UI, not only in a terminal the operator may never run.

### Other first-run messages worth pre-writing

- **Port already in use:** name the port, name the likely occupant (`5432` → "another Postgres,
  probably Homebrew or a system service"), and offer the override env var.
- **Stale Postgres volume from a different major version:** PG refuses to start with a message most
  people cannot parse. Detect `PG_VERSION` mismatch and say: "This volume was created by PostgreSQL
  17; the image is 18. Either restore from a dump or `docker compose down -v` to discard."
- **Disk space:** ClickHouse and Postgres both fail obscurely when the disk is full. Check before
  boot when the `tracing` profile is on.
- **A stopped dependency:** `docker compose ps` output plus `.HealthStatus` for anything not
  `running/healthy`, with the last 20 log lines of the first unhealthy service inlined. That one
  behaviour removes most "it doesn't work" reports.

---

## 12. On the performance worry about plugins

The owner's concern is that a plugin architecture will be slow. Compose is not where that risk lives,
and it is worth saying so explicitly so it does not distort the deployment design:

- **Do not map plugins onto Compose services.** One container per plugin turns every in-process call
  into a network hop plus serialisation, adds a container's memory floor per plugin, and makes
  "activate a plugin at runtime" a `compose up` — which is the slow architecture the owner is
  worried about, made literal. Aleph's kernel model (capabilities resolved in-process, revertible
  effects) has no Compose consequence at all; a plugin is a code-loading and dispatch question.
- The two levers Compose *does* give you: **fewer containers at rest** (profiles — the difference
  between 7 and 13 containers idling is several GB), and **`develop.watch` instead of bind mounts**
  for iteration speed.
- Keep the code-runner's isolation as-is. That is a security boundary that genuinely warrants a
  separate container, and it is the exception that proves the rule.

---

## Unverified / low-confidence items

- **Compose CLI v5 vs v2.40.** Sources conflict (§1). I trust v5, but Aleph should assert its
  minimum at first run rather than assume.
- **`pgvector:0.8.6-pg18-trixie` exact tag string.** The tag family and the 0.8.6 release date
  (29 Jul 2026) are confirmed; I did not pull the image to confirm that exact suffix. Verify with
  `docker manifest inspect` before committing it.
- **Chainguard MinIO free-tier pinning policy.** Confirmed free and used by Langfuse; I did not
  confirm which historical tags remain pullable on the free tier.
- **RustFS maturity.** Appears in every 2026 "MinIO alternative" list; I did not evaluate it.
- **Bifrost's benchmark claims** (~50x lower P99 than LiteLLM) come from its own vendor. Directionally
  interesting for "the gateway is interchangeable"; not a basis for a recommendation.

---

## What Aleph should do

1. **Name the file `compose.yaml`, delete `version:`, and put it at `deploy/compose.yaml`.** One
   file, profiles for optional capability, one auto-loaded `compose.override.yaml` for hacking mode.
   Make the `aleph` wrapper pass `-f compose.yaml` explicitly when running for real.
2. **Make the model endpoint one string, and make that string work everywhere.** Apply
   `extra_hosts: ["host.docker.internal:host-gateway"]` via a YAML anchor to every service that talks
   to models. Rename `LITELLM_BASE_URL` → `ALEPH_MODEL_ENDPOINT_URL` and
   `INSIGHTS_LITELLM_API_KEY` → `ALEPH_MODEL_ENDPOINT_KEY`; the endpoint is interchangeable and the
   variable names should say so.
3. **Shrink the default stack.** Default = postgres, valkey, api, workers, web, runtime, code-runner
   + its queue. Everything else (`tracing`, `s3`, `local-llm`, `gpu`, `tools`) behind a profile, and
   make sure the app runs correctly with tracing off — and test that.
4. **Support `_FILE` on every secret**, default the shipped compose to Compose `secrets:` mounted at
   `/run/secrets`, and keep plain env as the dev fallback. Generate all random secrets on first run
   so the operator supplies two values, not eight.
5. **Keep the one-shot `migrate` service** gated by `service_completed_successfully`. Adopt
   `pre_start` for genuinely once-only setup, and only move migrations there when dev stops
   bind-mounting source — the "not re-run unless the definition changed" rule is a real trap.
6. **Every service gets a readiness healthcheck**, using `start_period` + `start_interval`. Point
   the API's at the existing `/readyz`. Then `docker compose up -d --wait --wait-timeout 180` becomes
   a genuine "the stack is up" signal for both first run and CI.
7. **Do not gate readiness on the model endpoint.** Boot, then show its status prominently in the UI.
   Move `verify-gateway.sh`'s list-then-probe logic into a containerised doctor so it tests the same
   network path Aleph uses, and write the eight-step diagnosis in §11 as real strings.
8. **Pin full image tags**: `pgvector/pgvector:0.8.6-pg18-trixie` mounted at `/var/lib/postgresql`,
   Valkey 9 for the queues, ClickHouse 25.8 or 26.3 LTS (Langfuse needs ≥25.12), OTel collector
   0.158.0, Langfuse `:4`. Named volumes for all database state; `pg_dump` for backups.
9. **Keep `mem_limit` + `memswap_limit`** with the incident comment intact, since `deploy.resources`
   cannot express "no swap" — and write down that this is a deliberate exception.
10. **Use `develop.watch`** (`sync` for source, `rebuild` for `pyproject.toml`/`package.json`,
    `sync+restart` for config) instead of bind-mounting source, and run `docker compose up --watch`.

## What Aleph should avoid

1. **`network_mode: host`** as the answer to reaching the host's model server. It is Linux-only in
   any useful form, breaks service DNS between Aleph's own containers, and removes port isolation.
   Escape hatch only, documented as such.
2. **Compose's top-level `models:` element / Docker Model Runner.** It is well-designed and it is
   exactly the constraint Aleph exists to respect. Using it would make Aleph a model server. Say in
   the docs that this is deliberate.
3. **`minio/minio` community images.** The project archived on 12 Feb 2026 and stopped publishing
   community images in Oct 2025. Aleph's old file pins one in three places. Default to the filesystem
   asset backend; if an in-project store is wanted, use `cgr.dev/chainguard/minio` or Garage.
4. **Running the whole Langfuse stack by default.** Five containers and several GB for tracing that
   most first-run operators do not want yet. It is also how you end up hand-tuning a Node heap limit
   to stop a crash loop on a laptop.
5. **Migrations in the app entrypoint**, and `pre_start` migrations while dev bind-mounts source.
   Both produce "the schema is behind the code" with a success-looking boot.
6. **Liveness checks masquerading as readiness.** `depends_on: service_healthy` is worth nothing on
   top of a check that only proves a PID exists — and it produces exactly the class of silent,
   green-looking failure this codebase already has a documented allergy to.
7. **Putting anything secret in `environment:`** that could sit in a `_FILE`. It lands in
   `docker inspect` and therefore in the next pasted bug report.
8. **Assuming the operator's Compose is v5**, or setting `COMPOSE_BAKE` yourself. Assert the minimum
   version at first run; take Bake's default and treat the open `COMPOSE_BAKE` bugs as a known
   hazard.
9. **One container per plugin.** That is the slow plugin architecture the owner fears, made real in
   YAML. Plugins are a kernel and code-loading concern, not a Compose concern.
10. **`version:`, `links:`, `docker-compose.yml` as a filename, and `provider:` services.** All
    obsolete; all still present in copy-pasted examples across the web.

---

## Sources

Docker Compose releases (GitHub API, v5.5.0 / v5.4.0 / v5.3.0 / v5.0.0) ·
[Init containers in Compose](https://docs.docker.com/compose/how-tos/init-containers/) ·
[Services reference — `pre_start`](https://docs.docker.com/reference/compose-file/services/) ·
[`develop.watch` reference](https://docs.docker.com/reference/compose-file/develop/) ·
[Compose Watch how-to](https://docs.docker.com/compose/how-tos/file-watch.md) ·
[Profiles](https://docs.docker.com/compose/how-tos/profiles/) ·
[Interpolation](https://docs.docker.com/reference/compose-file/interpolation/) ·
[Compose deploy spec](https://docs.docker.com/reference/compose-file/deploy/) ·
[GPU support](https://docs.docker.com/compose/how-tos/gpu-support.md) ·
[Compose models element](https://docs.docker.com/reference/compose-file/models/) ·
[Docker Engine 29 release notes](https://docs.docker.com/engine/release-notes/29/) ·
[PostgreSQL 18.6 / 19 Beta 3 announcement](https://www.postgresql.org/about/news/postgresql-186-1711-1615-1519-1424-and-19-beta-3-released-3365/) ·
[pgvector tags](https://github.com/pgvector/pgvector/tags) ·
[Langfuse self-hosting (Docker Compose)](https://langfuse.com/self-hosting/deployment/docker-compose) ·
[Langfuse v3→v4 upgrade](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4) ·
[Langfuse OTLP ingestion](https://langfuse.com/integrations/native/opentelemetry) ·
[MinIO is dead, long live MinIO](https://blog.vonng.com/en/db/minio-resurrect/) ·
[Chainguard on MinIO image changes](https://www.chainguard.dev/unchained/secure-and-free-minio-chainguard-containers) ·
[Valkey vs Redis 2026](https://devops-daily.com/posts/is-valkey-ready-to-replace-redis-2026) ·
[host.docker.internal on Linux](https://hostim.dev/blog/fixing-host-docker-internal-linux/) ·
[Compose tip: `up --wait`](https://lours.me/posts/compose-tip-051-up-wait/) ·
[Remove obsolete `version` keys](https://adamj.eu/tech/2025/05/05/docker-remove-obsolete-compose-version/) ·
[LiteLLM alternatives 2026](https://www.getmaxim.ai/articles/5-best-open-source-llm-gateways-for-self-hosted-deployments-in-2026/)
