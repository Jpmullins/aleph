# Deployment

## Local (Docker Compose)

```bash
cp deploy/compose/.env.example deploy/compose/.env
# edit and fill secrets — at minimum INSIGHTS_LITELLM_API_KEY and the
# LANGFUSE_* random values (generate via `openssl rand -hex 32`)

./scripts/bootstrap-local.sh
```

The stack:

- `postgres` — Postgres 18 with pgvector
- `minio` — S3-compatible object store; console at http://localhost:9001
- `redis` — Arq broker + idempotency cache
- `langfuse` — trace + eval UI at http://localhost:3000
- `otel-collector` — receives OTLP from API + workers; forwards to Langfuse
- `aleph-api` — FastAPI at http://localhost:8000
- `aleph-workers` — Arq workers (no external port)
- `aleph-web` — React dev server at http://localhost:5173

## Production

Production deployment topology is not in scope for Increment 0. The compose stack
is the operational template; a k8s manifest set lands when production cutover is
in scope.
