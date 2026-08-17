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

## Running against local models

`LiteLLMClient` posts chat **and** embeddings to a single `LITELLM_BASE_URL`. A
bare vLLM serves chat only, so pointing Aleph straight at one breaks ingest at
the embed step — and a source that never chunks is invisible to *both* legs of
retrieval, not just the dense one. `deploy/local-gateway/` closes that gap: one
OpenAI-compatible surface, chat proxied to vLLM, embeddings served in-process by
bge-m3 on CPU.

```bash
docker compose -f deploy/compose/docker-compose.yml --profile local-llm up -d aleph-local-gateway
```

Then in `deploy/compose/.env`:

```
LITELLM_BASE_URL=http://host.docker.internal:8010   # containers; use localhost:8010 from the host
INSIGHTS_LITELLM_API_KEY=local-no-auth              # required non-empty; the gateway does not check it
ALEPH_FALLBACK_AGENT_MODEL=qwen3.8-27b-uncensored
```

Point the upstream elsewhere with `ALEPH_LOCAL_GATEWAY_UPSTREAM`. The embedder
is pulled from the local HuggingFace cache (`HF_HUB_OFFLINE=1`), so fetch it once
before first start.

Three things must line up, and `acceptance.sh::H1` checks all three:

1. **Every `ModelProfile` binding names a model the gateway serves.** Rebind with
   SQL against `model_profiles.bindings_jsonb`; templates included, or new
   projects inherit the old models.
2. **The embedder's width equals `EMBEDDING_DIM`** (1024). bge-m3 matches, so no
   migration is needed; a different embedder means re-dimensioning the column and
   re-embedding.
3. **The model is priced.** `aleph_models.pricing` must know it or the `models`
   probe fails boot. Local models are priced at zero — the honest per-token rate
   for owned hardware. Token *counts* are still ledgered.

`network_mode: host` is required because vLLM binds `127.0.0.1`; containers reach
the gateway via the `host.docker.internal` mapping on `aleph-api`/`aleph-workers`.

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
| `python-quality` | `ruff check` · `ruff format --check` · `pyright` (strict, 0 errors) · `check-catalog-generated.sh` · `check-graph-state-keys.sh` |
| `python-unit` | `pytest -m "not integration"` |
| `python-integration` | postgres + redis services · `alembic upgrade head` · `alembic check` · `pytest -m integration` |
| `web` | `pnpm lint` · `pnpm build` · `npm ci` + `tsc --noEmit` for `apps/copilot-runtime` |

Run them locally with the commands in [`CLAUDE.md`](../CLAUDE.md#commands).

### On what is deliberately absent

There is **no eval gate**. There was one; it graded a fixture against itself — scorers read both
`expected` and `actual` out of the same JSONL line, so the system under test was never invoked and the
job was green regardless of the code. The datasets and the gate have been deleted. The scorers in
`packages/aleph-evals/src/aleph_evals/scorers/` are kept because the metrics themselves are correct;
they need a harness that actually calls Aleph plus a real dataset before they mean anything.

The five `scripts/check-*.sh` "living invariant" sweeps have also been deleted. Two of them read files
that no longer exist, and the set was presented as equivalent invariants when one was a two-token
grep. When a real invariant needs enforcing, add a check that can fail for a real reason.

Two `scripts/check-*.sh` remain, and they are held to exactly that rule rather than grandfathered
past it — each regenerates an artifact from its source and diffs, so each has a concrete failing
input:

- `check-catalog-generated.sh` — `apps/web/src/a2ui/catalog.ts` and
  `apps/copilot-runtime/src/catalog.generated.ts` must match what `scripts/gen_catalog.py` renders
  from `catalog.json`. Fails on a hand-edit to a generated file, or on a `catalog.json` change
  committed without re-running the generator.
- `check-graph-state-keys.sh` — every key a LangGraph node writes must be declared on its state
  `TypedDict`; undeclared writes are discarded silently. It imports the analyzer from
  `tests/unit/test_graph_state_keys.py` rather than copying it, so the sweep and the behavioural
  tests cannot disagree.

## Observability

OTEL spans are exported through `otel-collector`; traces land in Langfuse (`:3000`). Spans follow
`<subsystem>.<op>`. LLM calls carry a `purpose` and are linked to `ModelCall` + `CostLedgerEvent` rows.

## Auth in deployment

Local mode is the compose default and skips user JWT verification entirely. Deploying means flipping
`ALEPH_AUTH_MODE=oidc` and setting `ALEPH_AUTH_ISSUER`, `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL`
(plus `VITE_AUTH_MODE` for the frontend). Do not deploy without also closing the agent-endpoint gap
recorded in [`../CLAUDE.md`](../CLAUDE.md#known-broken--do-not-trust-these).
