# Runbook

## Starting / stopping

```bash
# start
./scripts/bootstrap-local.sh

# stop
docker compose -f deploy/compose/docker-compose.yml down

# stop + wipe data (destructive)
docker compose -f deploy/compose/docker-compose.yml down -v
```

## Common diagnostics

### API not responding

```bash
docker compose -f deploy/compose/docker-compose.yml logs --tail 200 aleph-api
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz | jq
```

`/readyz` returns per-component status. The first false `ok` is the cause.

### Gateway unreachable

`/readyz` reports `litellm_gateway: { ok: false }`.

```bash
./scripts/verify-gateway.sh
```

Errors:

| Error | Fix |
|---|---|
| `Bearer` rejected | Rotate `INSIGHTS_LITELLM_API_KEY` |
| Required model missing | Confirm gateway upstream has the model; update `ALEPH_DEFAULT_MODEL_PROFILE` bindings if it was renamed |
| Timeout | Network egress; check VPN / firewall |

Project creation continues to work without the gateway — only LLM ops fail.

### Postgres up but migrations missing

```bash
cd apps/api && DATABASE_URL=postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph \
  uv run alembic upgrade head
cd apps/api && DATABASE_URL=... uv run alembic check
```

`alembic check` should produce no diff.

### Ledger chain integrity

Tamper detection (manual, rarely run):

```sql
SELECT id, action_kind, prev_event_id, chain_hash, timestamp
FROM action_ledger_events
WHERE project_id = '<id>'
ORDER BY timestamp;
```

Recompute each row's hash off-line; mismatch ⇒ rotate keys + investigate.

### Budget exceeded

`POST /v1/projects/{id}/smoke/llm` returns `429 Budget exceeded`.

```sql
SELECT cap_usd, spent_usd, soft_pct, hard_pct FROM budgets WHERE project_id = '<id>';
```

To raise the cap (owner-only via API):

```bash
curl -X PATCH http://localhost:8000/v1/projects/<id> \
  -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{}'   # PATCH /budgets endpoint lands in Inc 2 (budget UI); for now,
            # update via psql or write a follow-up route.
```

## Rotating secrets

- `INSIGHTS_LITELLM_API_KEY` — coordinate with gateway operator; update
  `.env`, restart `aleph-api` and `aleph-workers`.
- `ALEPH_AGENT_TOKEN_SECRET` — invalidates all live agent tokens.
  Update `.env`, restart all services. Long-running agent jobs must be
  re-minted; the API logs which jobs are affected.
- Postgres password — see Postgres operator docs; update `.env` and
  restart everything.

## Where the traces live

Langfuse at http://localhost:3000. Search by `aleph.project_id` or
`aleph.purpose` attributes.
