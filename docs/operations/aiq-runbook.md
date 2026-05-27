# AIQ runbook

## Bringing AIQ up

The AIQ server is vendored as a git submodule at `vendor/aiq`. Until
the submodule is checked out, the `aiq-server` compose service does
not start.

```bash
# One-time: vendor AIQ at the current release tag.
TAG=$(gh release list -R NVIDIA-AI-Blueprints/aiq --limit 1 | awk '{print $1}')
git submodule add -b $TAG https://github.com/NVIDIA-AI-Blueprints/aiq vendor/aiq
git submodule update --init --recursive

# Bump to a newer release later:
./scripts/update-aiq.sh   # follow-on script bumps the submodule + runs CI
```

After the submodule is in place, restart the compose stack — the
`aiq-server` service picks up `vendor/aiq` as its build context.

## Diagnostics

```bash
# Health
curl -H "Authorization: Bearer $AIQ_SERVICE_TOKEN" http://localhost:8001/v1/health

# Logs
docker compose -f deploy/compose/docker-compose.yml logs --tail 200 aiq-server

# Disposable: inspect AIQ's effective config
docker compose -f deploy/compose/docker-compose.yml exec aiq-server cat /etc/aiq/config.yml
```

## Without AIQ vendored

`/v1/projects/{id}/synthesize` still works: it creates an `AgentRun`
in `pending` and returns `dispatched=false`. The proposal lifecycle
machinery is fully tested without AIQ — once the submodule is in
place, the dispatch starts succeeding.

## Rotating the AIQ service-token secret

The service token is signed with `ALEPH_AGENT_TOKEN_SECRET` (shared
with worker agent tokens). Rotating it invalidates in-flight AIQ jobs;
they need to be retried.
