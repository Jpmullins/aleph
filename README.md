# Aleph

Living multi-agent research environment. See [`CLAUDE.md`](CLAUDE.md) and the [`docs/`](docs) doc set (architecture, research-loop, workspace, wiki, storage, operations, security) for how the system works, and [`docs/implementation-log.md`](docs/implementation-log.md) for the append-only record of what shipped.

Core loop: create a project → ask the assistant to research a topic (or upload a source) → a native deep-research loop (`aleph-research`: plan → search → ingest → reflect → compose) fans out across the project's allowed connectors + verified scholarship (`aleph-scholar`), and the wiki agent compiles a cited draft page → approve it in Briefs → query it conversationally, run ACH on competing hypotheses, take notes and promote them to the wiki. Wiki pages carry a freshness score and retraction awareness; agent-generated charts render as sandboxed artifacts.

## Quick start

```bash
# one-time
cp deploy/compose/.env.example deploy/compose/.env
# edit deploy/compose/.env and set INSIGHTS_LITELLM_API_KEY plus other secrets

# boot the local stack
./scripts/bootstrap-local.sh
```

Then:
- Web UI: http://localhost:5173
- API: http://localhost:8000
- CopilotKit runtime (Live chat bridge): http://localhost:4000
- Langfuse: http://localhost:3000

The default asset backend is the local filesystem (`data/assets`). An S3-compatible object store is opt-in: `docker compose --profile s3 up -d` adds it, with its console at http://localhost:9001.

## Repo layout

```
apps/
  api/             FastAPI app (+ in-process AG-UI Deep Agent)
  web/             React + Vite app
  workers/         Arq workers (incl. the native research loop, curator, wiki refresh)
  code-runner/     Sandboxed, credential-less worker that executes agent-written Python
  copilot-runtime/ Node @copilotkit/runtime v2 bridge (Live chat → AG-UI)
packages/
  aleph-core/         shared domain primitives + Pydantic schemas
  aleph-db/           SQLAlchemy models + repositories + Alembic
  aleph-security/     auth, Principal, role gates, agent tokens
  aleph-observability/  OTEL + Langfuse + structlog
  aleph-models/       LiteLLM transport + ModelProfile resolver + pricing
  aleph-scholar/      verified scholarship (Crossref/OpenAlex/Consensus, DOI verification)
  aleph-rks/          Raw Knowledge Store
  aleph-wiki/         wiki compile / curator / freshness / HTML compiler
  aleph-assistant/    chat orchestration + wiki retrieval router
  aleph-connectors/   typed connector plugins (driven by the research loop)
  aleph-research/     native deep-research LangGraph loop
  aleph-a2ui/         A2UI catalog + SDK glue
  aleph-reviewer/     reviewers + source retraction/blast-radius
  aleph-hypotheses/   analyst hypotheses
  aleph-datasets/     datasets + observations
  aleph-artifacts/    Builder agent + rendered assets + exporters
  aleph-notes/        analyst notebook
  aleph-evals/        eval runner + CI gates
deploy/compose/   docker-compose stack (fs default; --profile s3 for the object store)
docs/             architecture, research-loop, workspace, wiki, storage, operations, security
scripts/          bootstrap-local.sh, verify-gateway.sh, the committed CI sweeps
tests/e2e/        cross-package integration tests
```

See [`docs/architecture.md`](docs/architecture.md) for the full package list + strict DAG.

## Development

```bash
uv sync                          # install Python deps
pnpm install                     # install JS deps
ruff check .                     # lint
ruff format --check .            # format check
pyright                          # typecheck Python
pnpm -C apps/web typecheck       # typecheck TS
pytest -m "not integration"      # unit tests
pytest -m integration            # integration tests (requires compose stack)
```

## License

See [`LICENSE`](LICENSE).
