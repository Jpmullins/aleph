# Implementation log

## Increment 0: Foundations, ledger, cost spine, LiteLLM transport

**Completed:** 2026-05-27
**Commit range:** (will be filled at merge time)

### What was built

- Monorepo (`uv` + `pnpm` workspaces): `apps/api`, `apps/workers`, `apps/web`,
  plus six Python packages (`aleph-core`, `aleph-db`, `aleph-security`,
  `aleph-observability`, `aleph-models`, `aleph-evals`).
- Docker Compose stack: Postgres 18 with pgvector 0.8.2, MinIO,
  Redis 8.8, Langfuse 3.175, OTEL collector 0.153, plus the three
  Aleph apps.
- FastAPI app (`aleph-api`) with auth middleware (OIDC JWT + agent tokens),
  request-id middleware, error → RFC 7807 middleware, project-scope dep
  that returns 404 on non-membership.
- React 19 + Vite 8 + Tailwind 4 SPA with the 3-panel shell, project
  list, project create modal, cost banner, OIDC PKCE flow.
- Arq worker shell with a smoke job that round-trips a prompt through
  the gateway via an agent token.
- Alembic initial migration creating every Inc 0 table, the
  `pgvector` and `pgcrypto` extensions, the ledger-immutability triggers,
  the budget-rollup trigger, and the two seeded `ModelProfile` templates.
- `LiteLLMClient` (single chokepoint): chat + embed + health + list_models,
  tenacity retry, pricing-aware cost calc, ModelCall + CostLedgerEvent
  insert per call, Redis-backed idempotency cache, OTEL span per call.
- `LedgerWriter` with per-project chain head and sha256-chained events.
- `JWKSCache`, `verify_user_jwt`, `mint_agent_token`/`verify_agent_token`,
  `ProjectRole` + `require_at_least`.
- OTEL + Langfuse + structlog wiring with FastAPI / httpx / SQLAlchemy
  instrumentations.
- Bootstrap script (`scripts/bootstrap-local.sh`) and gateway verifier
  (`scripts/verify-gateway.sh`).
- CI workflow with lint, typecheck, unit tests, integration tests on a
  CI Postgres + Redis, eval skeleton, web build.

### Key files

- `pyproject.toml`, `pnpm-workspace.yaml`, `.gitignore`, `ruff.toml`/`pyrightconfig.json` (in `pyproject.toml`)
- `deploy/compose/docker-compose.yml`, `.env.example`, `otel-collector-config.yaml`
- `packages/aleph-core/src/aleph_core/{ids,time,errors,schemas/*}.py`
- `packages/aleph-db/src/aleph_db/{base,session,models/*,repos/*}.py`
- `packages/aleph-security/src/aleph_security/{principal,jwt,agent_token,roles}.py`
- `packages/aleph-observability/src/aleph_observability/{tracing,langfuse_client,logging}.py`
- `packages/aleph-models/src/aleph_models/{client,profile,pricing,retry}.py`
- `packages/aleph-evals/src/aleph_evals/{runner,cli}.py`
- `apps/api/src/aleph_api/{main,settings,lifespan,deps,middleware/*,routes/*}.py`
- `apps/api/alembic/versions/20260527_1200_inc0_initial.py`
- `apps/workers/src/aleph_workers/{settings,arq,jobs/smoketest}.py`
- `apps/web/src/{App,main,components/*,lib/*}.{ts,tsx}`
- `scripts/{bootstrap-local.sh,verify-gateway.sh}`
- `.github/workflows/{ci.yml,eval.yml}`

### Migrations added

- `inc0_initial` — every Inc 0 table, the `pgvector` + `pgcrypto`
  extensions, immutability triggers on `action_ledger_events`, budget
  rollup trigger on `cost_ledger_events`, two `ModelProfile` template
  rows.

### Tests added

- Unit:
  - `aleph-core`: UUIDv7 version/variant + monotonicity + deterministic seed (test_ids); ProjectCreate / ModelBindingIn / ModelProfileUpdate (test_schemas).
  - `aleph-security`: agent token round-trip, wrong-secret rejection, TTL bounds (test_agent_token); role gate behavior (test_roles).
  - `aleph-models`: pricing table coverage + cache discount math (test_pricing); profile resolver (test_profile).
  - `aleph-db`: hash chain determinism + canonical JSON sort (test_ledger_chain).
  - `aleph-evals`: runner skeleton — empty/missing root passes strict; dataset discovery (test_runner).
- Integration (`tests/e2e/`):
  - `test_project_lifecycle.py` — create project, verify 4 ledger events with chain continuity.
  - `test_ledger_immutable.py` — UPDATE and DELETE both raise.
  - `test_permission_leakage.py` — user B sees user A's project as 404.
  - `test_smoke_llm.py` — patched gateway round-trip writes ModelCall + CostLedgerEvent and updates budget.

### Trace and ledger behavior added

- Ledger action kinds covered: `user.create`, `project.create`,
  `project.update`, `project_member.add`, `project_member.remove`,
  `project_member.role_change`, `budget.set`,
  `model_profile.copy_from_template`, `model_profile.update`,
  `agent_run.create`.
- OTEL spans: `litellm.chat`, `litellm.embed`, plus auto-instrumented
  FastAPI request spans, httpx outbound spans, SQLAlchemy query spans.
- Cost ledger covers all LLM and embedding calls through the gateway
  (every chat/embed inserts a `ModelCall` + `CostLedgerEvent`).

### Manual verification

- [ ] `scripts/bootstrap-local.sh` succeeds on a clean clone.
- [ ] `alembic upgrade head` then `alembic check` returns clean.
- [ ] Web at http://localhost:5173 renders the 3-panel shell.
- [ ] OIDC login → `/v1/me` returns the resolved Principal.
- [ ] `POST /v1/projects` writes 4 ledger events and creates Project, ProjectMember, ModelProfile, Budget atomically.
- [ ] `POST /v1/projects/{id}/smoke/llm` returns a chat completion + cost + trace id.
- [ ] Langfuse UI shows the trace tagged with `aleph.project_id` and `aleph.purpose=inc0.smoke`.
- [ ] `UPDATE action_ledger_events` raises; `DELETE` raises.
- [ ] User B receives 404 (not 403) when reading user A's project resources.
- [ ] With the gateway unreachable, `/readyz` returns 503 with `litellm_gateway.ok=false`; project creation still works.

### Known issues / debts

- **Real OIDC IdP not bundled.** The compose stack does not include Keycloak.
  Local UI development against the API uses the test-only `verify_user_jwt`
  monkey-patch. Inc 0's acceptance criteria assume the operator provides
  an OIDC IdP (or sets up Keycloak separately).
- **`alembic check`** depends on every model decorator landing in
  `Base.metadata` at import time — guarded by importing
  `aleph_db.models` in `apps/api/alembic/env.py`.
- **Web app routing** uses a homegrown `parseRoute` shim. TanStack
  Router is included in the dep list and will replace this in Inc 1
  alongside real navigation.
- **Cost-per-token rates** in `pricing.py` reflect published list
  prices as of 2026-05-27; the gateway operator may have negotiated
  per-tenant pricing. Adjust the table when negotiated rates land.
- **No budget-edit API route in Inc 0.** Owner can mint a new project
  with a higher cap, or raise via SQL. A `PATCH /budgets` route lands
  in Inc 2.

### Next increment entry point

See `docs/superpowers/specs/2026-05-27-inc-1-rks-wiki-skeleton-design.md`.
Increment 1 adds: RKS entities (`Source`, `SourceVersion`, `SourceAsset`,
`NormalizedDocument`, `DocumentChunk`), the Upload connector, the
normalization + chunking + embedding workers, the wiki entity skeleton
and the wiki ingest agent. Inc 1 introduces a new Alembic migration —
never edit `inc0_initial`.
