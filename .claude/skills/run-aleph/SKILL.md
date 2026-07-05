---
name: run-aleph
description: Run, start, screenshot, or drive the Aleph stack — boot the docker compose stack, smoke-check endpoints, open the web UI, send a chat message to the Live agent, or run the Playwright e2e suite. Use when asked to run Aleph, verify a change in the real app, or take a screenshot of the workspace.
---

# Run Aleph

Aleph is a docker-compose stack: FastAPI (`aleph-api` :8000), React/Vite web
(`aleph-web` :5173), arq workers, CopilotKit runtime (:4000), plus
postgres/minio/redis/langfuse/otel. The agent path to drive it is
**`node .claude/skills/run-aleph/driver.mjs <cmd>`** — a Playwright CLI that
reuses the install at `tests/playwright/node_modules`. All paths below are
relative to the repo root.

## Prerequisites

Docker + compose, `uv`, Node 24. The compose `.env` must exist at
`deploy/compose/.env` (gitignored; on this machine it was generated 2026-05-28
with real gateway keys — if missing, `cp deploy/compose/.env.example
deploy/compose/.env` and set `INSIGHTS_LITELLM_API_KEY`). All images are cached
locally.

Playwright deps (once):

```bash
cd tests/playwright && npm install        # @playwright/test; browsers already cached in ~/.cache/ms-playwright
```

## Boot

```bash
./scripts/bootstrap-local.sh   # infra + DBs + alembic + gateway check + api/workers/web (~1 min, images cached)
# bootstrap does NOT start the chat bridge — without it the Live agent is dead:
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env up -d aleph-copilot-runtime
```

Verify everything is up:

```bash
node .claude/skills/run-aleph/driver.mjs smoke
# ✓ api /healthz -> 200      ✓ web -> 200
# ✓ copilot-runtime -> 404   (404 is OK — no health route; any HTTP status = process up)
# ✓ api auth (Bearer local-dev) -> N projects
```

`smoke` polls each endpoint for up to 90s — containers report Started ~20–30s
before the api app actually listens, so run it right after boot and let it
wait.

## Drive the UI (agent path)

```bash
node .claude/skills/run-aleph/driver.mjs shot / /tmp/aleph-home.png        # screenshot any route
node .claude/skills/run-aleph/driver.mjs mkproject "My test"               # create a project via API, prints its id
node .claude/skills/run-aleph/driver.mjs workspace                         # open first project, assert 5 surface tabs, screenshot
node .claude/skills/run-aleph/driver.mjs chat "Say hello in five words."   # NEW session → send → wait for orchestrator reply (15–90s)
```

`workspace`/`chat` take an optional `projectId` (defaults to first project from
`GET /v1/projects`). Screenshots land in `/tmp/aleph-*.png` — **look at them**.
`chat` exits 1 if no assistant reply renders within 150s.

Direct API access (local auth mode maps any request to `dev@aleph.local`; the
sentinel bearer is required):

```bash
curl -s http://localhost:8000/v1/projects -H 'Authorization: Bearer local-dev' | head -c 300
```

## E2E suite (deeper flows)

`tests/playwright/specs/` covers project lifecycle, source→wiki, surfaces,
charts, progress, CopilotKit. Serial (`workers: 1`), shared ledger state,
300s per-test timeout — full runs are slow and cost real LLM tokens via the
gateway. Run one spec:

```bash
cd tests/playwright && npx playwright test specs/02-workspace-shell.spec.ts --reporter=list
```

⚠️ **The suite DELETES ALL PROJECTS in the local DB** — `helpers.ts
cleanupAllProjects()` runs in spec hooks and soft-deletes every project, not
just test ones (verified: a run took the DB from 357 projects to 0). Don't run
it against state you care about. Current result for spec 02: **3 passed,
5 failed** — the failures are the stale-`chat-composer` helpers (see Gotchas),
not a broken app.

## Run (human path)

`pnpm -C apps/web dev` (frontend hot-reload against the compose API) **errors
with `Port 5173 is already in use`** while the compose `aleph-web` is up — stop
that container first:

```bash
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env stop aleph-web
pnpm -C apps/web dev
```

## Stop

```bash
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env down
```

## Gotchas (all hit in anger)

- **`bootstrap-local.sh` skips `aleph-copilot-runtime`.** Chat silently does
  nothing without it (the runtime bridges web → `aleph-api`'s AG-UI agent
  endpoint). Always `up -d aleph-copilot-runtime` after bootstrap.
- **API health is `/healthz`, not `/health`** (`/health` → 404 `{"detail":"Not Found"}`).
- **Chat testids changed in W4/W6:** the composer is
  `copilot-chat-textarea`, send is `copilot-send-button`, transcript is
  `copilot-message-list`. `tests/playwright/specs/helpers.ts` still waits on a
  stale `chat-composer` testid — its `createSession`/`sendChat` helpers time
  out; specs that use them fail until updated. The driver uses the current ids.
- **Benign teardown noise:** closing the browser mid-session always prints
  `AbortError: BodyStreamBuffer was aborted` + `REQFAIL …/agent/assistant/connect`
  — that's the persistent AG-UI SSE stream being killed, not a failure.
- **`node /dev/stdin <<EOF` breaks on Node 24** (ENOENT on the proc fd pipe
  with ESM/await). Write throwaway scripts to a real file, or extend the driver.
- **Chat replies are slow.** A trivial no-tool turn takes ~15–30s through the
  Deep-Agents orchestrator; tool-using turns 60s+. Poll, don't fixed-wait.
- **Chromium missing shared libs (`libnspr4.so` etc.).** On this WSL2 box
  there's no passwordless sudo, so Playwright's `install-deps` can't run.
  The libs are staged in `~/.cache/ms-playwright/aleph-syslibs/` and
  `driver.mjs` prepends that to `LD_LIBRARY_PATH` at launch (no-op if the
  dir is absent). If the dir gets wiped, re-stage the NSS/NSPR/alsa `.so`s
  there (e.g. `apt-get download libnspr4 libnss3 libasound2t64` then
  `dpkg -x` each into a temp dir and copy the `.so*` files across).

## Troubleshooting

- `✗ Missing deploy/compose/.env` from bootstrap → copy `.env.example`, set
  `INSIGHTS_LITELLM_API_KEY`.
- `chat` exits 1 / empty transcript → check the bridge:
  `docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env logs --tail=20 aleph-copilot-runtime`
  (healthy = `listening on :4000/api/copilotkit → agent http://aleph-api:8000/...`),
  then `logs aleph-api` for the agent endpoint.
- `no projects` from driver → `node .claude/skills/run-aleph/driver.mjs mkproject`
  (likely cause: an e2e run wiped them — see the ⚠️ above).
- `✗ web -> DOWN (ECONNREFUSED)` but `docker compose ps aleph-web` says Up →
  the container restarted while something else (e.g. a host `pnpm dev`) held
  :5173; the port bind silently failed and `docker port compose-aleph-web-1`
  prints nothing. Fix:
  `docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env up -d --force-recreate aleph-web`
