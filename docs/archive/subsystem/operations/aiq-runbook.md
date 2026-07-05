# AIQ runbook

## Bringing AIQ up

`aiq-server` runs the **prebuilt NVIDIA image**
`nvcr.io/nvidia/blueprint/aiq-agent:2.1.0` (not a local build / submodule).
It requires an NGC login once per host:

```bash
echo "$NGC_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin
docker pull nvcr.io/nvidia/blueprint/aiq-agent:2.1.0
```

Boot config: `deploy/compose/aiq-config-default.yml` (LLM blocks `_type:
openai` → Insights gateway; a `data_source_registry` web-search source backed
by `tavily_web_search`, needs `TAVILY_API_KEY` on the service).

**Job-store schema is NOT auto-created by the image** (only `job_events` is).
`bootstrap-local.sh` applies `deploy/compose/aiq-init-{jobs,checkpoints}.sql`
to create `job_info` / `job_access` / `summaries` + LangGraph checkpoint
tables. Without `job_info`, every `/v1/jobs/async/submit` 500s with
"relation job_info does not exist".

> Upgrading the image: bump the tag in `docker-compose.yml`, `docker pull`,
> recreate `aiq-server`. (2.0.0 → 2.1.0 fixed a `ShallowResearchAgentConfig`
> `orchestrator_llm` crash.)

## Diagnostics

```bash
# Health (note: /health, NOT /v1/health; no auth needed in local mode)
curl http://localhost:8001/health

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
