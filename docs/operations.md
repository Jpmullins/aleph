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

## Migrations

Alembic lives in `apps/api`. Never edit an existing revision; add a new one.

```bash
cd apps/api
uv run alembic upgrade head
uv run alembic check                              # must produce no diff — CI enforces
uv run alembic revision -m "<slug>" --autogenerate
```

File pattern: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.

## Gates

CI (`.github/workflows/ci.yml`) runs four jobs, and each one can genuinely fail:

| job | what it runs |
|---|---|
| `python-quality` | `ruff check` · `ruff format --check` · `pyright` (strict, 0 errors) |
| `python-unit` | `pytest -m "not integration"` |
| `python-integration` | postgres + redis services · `alembic upgrade head` · `alembic check` · `pytest -m integration` |
| `web` | `pnpm lint` · `pnpm build` |

Run them locally with the commands in [`CLAUDE.md`](../CLAUDE.md#commands).

### On what is deliberately absent

There is **no eval gate**. There was one; it graded a fixture against itself — scorers read both
`expected` and `actual` out of the same JSONL line, so the system under test was never invoked and the
job was green regardless of the code. The datasets and the gate have been deleted. The scorers in
`packages/aleph-evals/src/aleph_evals/scorers/` are kept because the metrics themselves are correct;
they need a harness that actually calls Aleph plus a real dataset before they mean anything.

The former `scripts/check-*.sh` sweeps have also been deleted. Two of them read files that no longer
exist, and the set was presented as equivalent "living invariants" when one was a two-token grep.
When a real invariant needs enforcing, add a check that can fail for a real reason.

## Observability

OTEL spans are exported through `otel-collector`; traces land in Langfuse (`:3000`). Spans follow
`<subsystem>.<op>`. LLM calls carry a `purpose` and are linked to `ModelCall` + `CostLedgerEvent` rows.

## Auth in deployment

Local mode is the compose default and skips user JWT verification entirely. Deploying means flipping
`ALEPH_AUTH_MODE=oidc` and setting `ALEPH_AUTH_ISSUER`, `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL`
(plus `VITE_AUTH_MODE` for the frontend). Do not deploy without also closing the agent-endpoint gap
recorded in [`../CLAUDE.md`](../CLAUDE.md#known-broken--do-not-trust-these).
