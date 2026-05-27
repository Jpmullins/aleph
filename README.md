# Aleph

Living multi-agent research environment. See [`docs/superpowers/specs/2026-05-26-aleph-design.md`](docs/superpowers/specs/2026-05-26-aleph-design.md) for the design.

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
- Langfuse: http://localhost:3000
- MinIO console: http://localhost:9001

## Repo layout

```
apps/
  api/        FastAPI app
  web/        React + Vite app
  workers/    Arq workers
packages/
  aleph-core/         shared domain primitives + Pydantic schemas
  aleph-db/           SQLAlchemy models + repositories + Alembic
  aleph-security/     auth, Principal, role gates, agent tokens
  aleph-observability/  OTEL + Langfuse + structlog
  aleph-models/       LiteLLM transport + ModelProfile resolver + pricing
  aleph-evals/        eval runner skeleton
deploy/compose/   docker-compose stack
docs/             specs, engineering, runbooks
scripts/          bootstrap-local.sh, verify-gateway.sh
tests/e2e/        cross-package integration tests
```

See [`docs/engineering/repo-structure.md`](docs/engineering/repo-structure.md) for details.

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
