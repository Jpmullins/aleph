# AIQ trust boundary

AIQ is a separate process. Aleph treats it as untrusted with respect
to database and object-store access: AIQ holds no Postgres URL, no
MinIO credentials, no provider API keys.

## What AIQ has

- A signed **service token** (HS256, issued by aleph-api) bound to a
  specific `AgentRun`, `project_id`, and `principal_user_id`. TTL ≤ 1h.
- The Insights LiteLLM Gateway bearer (so AIQ can call models — the
  cost is logged to the ledger via the tokenomics adapter).
- An `aleph-api` base URL for the `/internal/v1/aiq/*` callbacks.
- An OTEL collector endpoint so AIQ spans flow into Langfuse with
  Aleph's.

## What AIQ does NOT have

- No `DATABASE_URL`.
- No MinIO root credentials.
- No connector API keys (it fetches them per-call via the credentials
  callback, scoped to the project).

## Callback verification

Every `/internal/v1/aiq/*` route extracts `X-Aleph-Service-Token` and
verifies it via `aleph_aiq.auth_bridge.verify_service_token`. The
verified claims (`project_id`, `agent_run_id`, `principal_user_id`)
are the authorization context for the call. Any mismatch → 401.

## Egress restriction

The compose network is configured so `/internal/*` is reachable only
by `aiq-server`. The public load balancer does not route `/internal/*`.

## What this prevents

- AIQ cannot read or write rows from another project — the service
  token scopes it to one.
- AIQ cannot exfiltrate API keys to disk; they live in memory only
  for the duration of a single connector call.
- AIQ cannot bypass the ledger; every state change goes through a
  callback that writes the ledger event.
