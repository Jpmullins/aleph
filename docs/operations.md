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

## Running against any OpenAI-compatible endpoint

Aleph serves no models. It connects out to whatever OpenAI-compatible endpoint is
configured — LiteLLM, Ollama, vLLM, Bedrock behind a proxy — and discovers what
that endpoint serves at runtime. It ships no model list and no price list.

```
# deploy/compose/.env
LITELLM_BASE_URL=https://your-gateway.example.com
LITELLM_API_KEY=...
```

Two things to know before pointing it somewhere new.

**`LiteLLMClient` posts chat *and* embeddings to the same base URL.** A bare vLLM
serves chat only, so an endpoint without an embeddings route breaks ingest at the
embed step. That failure is worse than it sounds: chunks are written only *after*
the embed call returns, so a missing embedder also kills the lexical leg of
retrieval, which needs no model at all. The source ingests, produces no chunks,
and becomes invisible to both legs. Put something in front that serves both.

**The bound model name must match what the endpoint actually serves.** This is
not hypothetical — production currently has `document_chunks` at 0 rows against
75 sources because the profile binds `titan-embed-v2` while the gateway serves
`titan-embed-text-v2`. Check with:

```bash
curl -s localhost:8000/v1/gateway/models | python3 -m json.tool
```

> The old `deploy/local-gateway/` directory referenced by earlier versions of
> this document was deleted. There is no bundled gateway.


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
