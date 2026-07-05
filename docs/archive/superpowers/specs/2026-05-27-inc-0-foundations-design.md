# Increment 0 — Foundations, Ledger, Cost Spine, LiteLLM Transport

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md` (top-level, §13)
**Status:** Subsystem design spec for Increment 0.
**Written:** 2026-05-27.

## 0.1 Scope

Increment 0 builds the durable spine of Aleph: the monorepo, the infrastructure, the auth boundary, the action ledger, the cost ledger, the LiteLLM transport, the trace context, and the seeded `ModelProfile`s. No LLM-facing user functionality lands here. The wiki, sources, assistant chat, A2UI surfaces — all later. **What this increment guarantees is that everything built on top of it will be traced, ledgered, cost-tracked, project-scoped, and route LLM calls through one chokepoint.**

This spec is the brief for one fresh coding-agent session. It is self-contained: a coding agent that loads only this file (plus the top-level spec for context) can execute it.

### In scope

- Monorepo layout
- Docker Compose: Postgres + `pgvector`, MinIO, Redis, Langfuse, OTEL collector
- FastAPI app shell (`aleph-api`)
- React + Vite app shell (`aleph-web`) — minimal: empty workspace with project switcher
- Worker shell (`aleph-workers`) using Arq + Redis
- Alembic migrations framework
- Auth middleware + `Principal` resolution (OIDC + JWT)
- Models: `User`, `Project`, `ProjectMember`, `ActionLedgerEvent`, `AgentRun`, `AgentEvent`, `ModelProfile`, `ModelCall`, `CostLedgerEvent`, `Budget`
- LiteLLM transport client (single chokepoint)
- Action ledger writer service
- Cost ledger writer service
- Langfuse + OTEL trace context
- Two seeded `ModelProfile`s (`aleph-dev`, `aleph-production`) + env-driven selection
- One-command local boot
- CI: lint, typecheck, test, migration check, eval-runner skeleton
- A smoke test: `POST /v1/chat/completions` through the gateway is traced and ledgered
- Docs

### Explicitly out of scope (in later increments)

- Sources, normalization, chunking, embeddings → Increment 1
- Wiki entities, wiki agent, source pages → Increment 1
- Assistant chat → Increment 2
- AIQ integration → Increment 3
- A2UI surfaces → Increment 4
- Reviewer agents → Increment 5
- Datasets, charts → Increment 6
- Builder, artifacts → Increment 7
- Eval suite → Increment 8

### Dependencies

None. This is the root.

### What downstream increments rely on

Every subsequent increment depends on: the auth + `Principal` boundary; the action ledger writer; the cost ledger writer; the LiteLLM client; the trace context; the migration framework; the Arq worker shell; the `Project`/`ProjectMember` schema; the seeded `ModelProfile` rows.

---

## 0.2 Repository layout

Top-level monorepo. Single Git repo (`github.com/UMD-ARLIS/aleph`). Workspaces managed with `uv` (Python) and `pnpm` (JS).

```
aleph/
├── apps/
│   ├── api/                            # FastAPI app
│   │   ├── pyproject.toml
│   │   ├── alembic.ini
│   │   ├── alembic/
│   │   │   ├── env.py
│   │   │   ├── script.py.mako
│   │   │   └── versions/
│   │   │       └── <timestamp>_inc0_initial.py
│   │   └── src/aleph_api/
│   │       ├── __init__.py
│   │       ├── main.py                 # FastAPI app factory
│   │       ├── lifespan.py             # startup/shutdown
│   │       ├── settings.py             # Pydantic-settings
│   │       ├── middleware/
│   │       │   ├── auth.py             # JWT verify + Principal resolution
│   │       │   ├── project_scope.py    # project_id resolution
│   │       │   └── request_id.py
│   │       ├── routes/
│   │       │   ├── health.py           # GET /healthz, /readyz
│   │       │   ├── projects.py         # CRUD on Project
│   │       │   ├── ledger.py           # GET ledger by project
│   │       │   ├── cost.py             # GET cost rollup by project
│   │       │   └── smoketest.py        # POST /v1/smoke/llm
│   │       └── deps.py                 # FastAPI dependencies
│   ├── web/                            # React + Vite shell
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   ├── vite.config.ts
│   │   ├── tailwind.config.ts
│   │   ├── index.html
│   │   └── src/
│   │       ├── main.tsx
│   │       ├── App.tsx                 # 3-panel shell (empty surfaces)
│   │       ├── lib/
│   │       │   ├── api.ts              # typed client wrapping fetch
│   │       │   └── auth.ts             # token handling
│   │       └── routes/
│   │           ├── index.tsx           # project list
│   │           └── project/$id.tsx     # empty workspace
│   └── workers/                        # Arq workers
│       ├── pyproject.toml
│       └── src/aleph_workers/
│           ├── __init__.py
│           ├── settings.py
│           ├── arq.py                  # WorkerSettings
│           └── jobs/
│               └── smoketest.py        # background smoke job
├── packages/
│   ├── aleph-core/                     # shared domain models + Pydantic schemas
│   │   ├── pyproject.toml
│   │   └── src/aleph_core/
│   │       ├── __init__.py
│   │       ├── ids.py                  # UUIDv7 generators, typed IDs
│   │       ├── time.py                 # tz-aware UTC utilities
│   │       ├── schemas/                # Pydantic models for API payloads
│   │       │   ├── project.py
│   │       │   ├── ledger.py
│   │       │   ├── cost.py
│   │       │   └── model_profile.py
│   │       └── errors.py
│   ├── aleph-db/                       # SQLAlchemy ORM + repo functions
│   │   ├── pyproject.toml
│   │   └── src/aleph_db/
│   │       ├── __init__.py
│   │       ├── base.py                 # declarative Base, common columns
│   │       ├── session.py              # async engine + sessionmaker
│   │       ├── models/
│   │       │   ├── identity.py         # User, ProjectMember
│   │       │   ├── project.py
│   │       │   ├── ledger.py           # ActionLedgerEvent
│   │       │   ├── agent.py            # AgentRun, AgentEvent
│   │       │   ├── cost.py             # ModelCall, CostLedgerEvent, Budget
│   │       │   └── model_profile.py    # ModelProfile, ModelBinding
│   │       └── repos/                  # data access (one module per aggregate)
│   │           ├── project.py
│   │           ├── ledger.py
│   │           ├── cost.py
│   │           └── model_profile.py
│   ├── aleph-security/                 # auth, JWT verify, Principal
│   │   └── src/aleph_security/
│   │       ├── __init__.py
│   │       ├── principal.py
│   │       ├── jwt.py
│   │       └── roles.py                # ProjectRole enum, gate helpers
│   ├── aleph-observability/            # Langfuse + OTEL + structlog
│   │   └── src/aleph_observability/
│   │       ├── __init__.py
│   │       ├── tracing.py              # OTEL setup
│   │       ├── langfuse_client.py
│   │       └── logging.py              # structlog config
│   ├── aleph-models/                   # LiteLLM client + ModelProfile resolver
│   │   └── src/aleph_models/
│   │       ├── __init__.py
│   │       ├── client.py               # LiteLLM client (chat + embed)
│   │       ├── profile.py              # capability → ModelBinding resolution
│   │       ├── pricing.py              # cost calc per call
│   │       └── retry.py                # tenacity retry policy
│   └── aleph-evals/                    # eval runner skeleton (no datasets yet)
│       └── src/aleph_evals/
│           ├── __init__.py
│           └── runner.py               # stub, expanded in Inc 8
├── deploy/
│   ├── compose/
│   │   ├── docker-compose.yml          # local stack
│   │   ├── .env.example
│   │   └── otel-collector-config.yaml
│   └── README.md
├── docs/
│   ├── superpowers/specs/              # design specs (this file lives here)
│   ├── engineering/
│   │   ├── local-development.md
│   │   ├── repo-structure.md
│   │   ├── quality-gates.md
│   │   └── litellm-transport.md
│   ├── domain/
│   │   └── ledger.md
│   ├── operations/
│   │   └── runbook.md
│   ├── security/
│   │   └── auth.md
│   └── implementation-log.md           # appended after each increment
├── scripts/
│   ├── bootstrap-local.sh              # one-command local boot
│   └── verify-gateway.sh               # gateway health + /v1/models
├── tests/                              # cross-package integration tests
│   └── e2e/
│       └── test_smoketest_llm.py
├── .github/workflows/
│   ├── ci.yml                          # lint + typecheck + test + migrations
│   └── eval.yml                        # eval suite (skeleton)
├── pyproject.toml                      # uv workspace root
├── pnpm-workspace.yaml
├── .gitignore
├── .editorconfig
├── ruff.toml
├── pyrightconfig.json                  # or mypy.ini
├── README.md
└── LICENSE
```

### 0.2.1 Workspace tooling

- **Python:** `uv` workspace. Each Python package has a `pyproject.toml`. Root `pyproject.toml` declares workspace members and pinned tool versions (`ruff`, `pyright` or `mypy`, `pytest`).
- **JS/TS:** `pnpm` workspace. Root `pnpm-workspace.yaml`. `apps/web` is the only JS workspace in Inc 0.
- **Versions in manifests** (verified 2026-05-27 against npm/PyPI registries; renovate-bot rolls forward per §15.6 of top-level spec):
  - Python: `>=3.13` (use 3.13.x; 3.14 acceptable)
  - FastAPI ~`0.136.3`, Pydantic ~`2.13.4`, SQLAlchemy ~`2.0.50`, Alembic ~`1.18.4`
  - uvicorn ~`0.48.0`, httpx ~`0.28.1`, tenacity ~`9.1.4`, orjson ~`3.11.9`
  - python-jose ~`3.5.0`, passlib ~`1.7.4`
  - opentelemetry-api ~`1.42.1`, langfuse ~`4.6.1`, structlog ~`25.5.0`
  - arq ~`0.28.0`
  - React ~`19.2.6`, react-dom ~`19.2.6`, TypeScript ~`6.0.3`, Tailwind ~`4.3.0`
  - Vite ~`8.0.14`, `@vitejs/plugin-react` ~`6.0.2`, zod ~`4.4.3`

Each manifest pins exact versions at install time and tracks upstream latest per the §15.6 policy. **The coding agent re-verifies these at the moment of writing the manifests** by hitting the registries — do not assume the versions above are still current at execution time.

---

## 0.3 Infrastructure (Docker Compose)

`deploy/compose/docker-compose.yml` defines the local stack. All services bind to localhost. Secrets via `.env`.

### Services

| Service | Image (track latest) | Purpose | Local port |
|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg18` or later | Domain DB + pgvector | 5432 |
| `minio` | `minio/minio:latest` | S3-compatible object store | 9000, 9001 (console) |
| `redis` | `redis:8-alpine` or later | Arq broker + pub/sub | 6379 |
| `langfuse` | `langfuse/langfuse:latest` | Trace + eval backend | 3000 |
| `otel-collector` | `otel/opentelemetry-collector-contrib:latest` | OTEL ingest, forwards to Langfuse | 4317 (gRPC), 4318 (HTTP) |
| `aleph-api` | built from `apps/api` | FastAPI app | 8000 |
| `aleph-workers` | built from `apps/workers` | Arq workers | — |
| `aleph-web` | built from `apps/web` (dev mode via `pnpm dev`) | React dev server | 5173 |

### Env vars (in `.env.example`)

```bash
# Project
ALEPH_ENV=local                            # local | dev | staging | prod
ALEPH_DEFAULT_MODEL_PROFILE=aleph-dev      # aleph-dev | aleph-production

# Postgres
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=aleph
POSTGRES_USER=aleph
POSTGRES_PASSWORD=changeme-local
DATABASE_URL=postgresql+asyncpg://aleph:changeme-local@postgres:5432/aleph

# MinIO
MINIO_ENDPOINT=http://minio:9000
MINIO_ROOT_USER=aleph
MINIO_ROOT_PASSWORD=changeme-local
ALEPH_S3_BUCKET=aleph-local

# Redis
REDIS_URL=redis://redis:6379/0

# Langfuse
LANGFUSE_HOST=http://langfuse:3000
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...

# OTEL
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# LiteLLM Gateway (the only LLM transport)
LITELLM_BASE_URL=https://gateway.insights.arlis.umd.edu
INSIGHTS_LITELLM_API_KEY=<set in .env, never committed>

# Connector dev-default keys (also live in .env, never committed)
TAVILY_API_KEY=
SEMANTIC_SCHOLAR_API_KEY=
ARTIFICIALANALYSIS_API_KEY=
OPENAI_API_KEY=
LENS_API_KEY=

# Auth (OIDC)
ALEPH_AUTH_ISSUER=http://localhost:8080/realms/aleph
ALEPH_AUTH_AUDIENCE=aleph
ALEPH_AUTH_JWKS_URL=http://localhost:8080/realms/aleph/protocol/openid-connect/certs
```

### `.gitignore`

Must include `.env`, `*.env.local`, build artifacts, `node_modules`, `__pycache__`, `.venv`, `.pytest_cache`, `.ruff_cache`, `.pyright`, MinIO data dirs, etc. Do **not** commit `API_keys.txt` ever.

---

## 0.4 Domain model — concrete schemas

All tables use UUIDv7 primary keys (time-ordered) generated via `aleph_core.ids.uuid7()`. Common columns are on every persistent table:

```python
# packages/aleph-db/src/aleph_db/base.py
from __future__ import annotations
from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class CommonColumns:
    """Mixin every persistent table inherits."""
    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
    created_by: Mapped[UUID] = mapped_column(nullable=False)  # FK to user.id
    access_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="project")
    # nullable; populated when the row was created in a traced/ledgered op
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
```

### 0.4.1 `User`

```python
# packages/aleph-db/src/aleph_db/models/identity.py
class User(CommonColumns, Base):
    __tablename__ = "users"
    subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)  # OIDC sub
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
```

`project_id` does not apply; `access_scope = "global"`. `created_by` self-references after row exists (use post-insert update or DB-side default; ledger event captures the JIT-provision moment).

### 0.4.2 `Project`

```python
class Project(CommonColumns, Base):
    __tablename__ = "projects"
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(4096), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # active | archived | deleted
    model_profile_id: Mapped[UUID] = mapped_column(nullable=False)  # FK model_profiles.id
    budget_id: Mapped[UUID | None] = mapped_column(nullable=True)
```

### 0.4.3 `ProjectMember`

```python
class ProjectMember(CommonColumns, Base):
    __tablename__ = "project_members"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # owner | editor | viewer

    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
```

### 0.4.4 `ActionLedgerEvent` — append-only, hash-chained

```python
class ActionLedgerEvent(Base):
    """Append-only audit log. Hash-chained for tamper evidence.

    NO updates. NO deletes. Compaction by archival.
    """
    __tablename__ = "action_ledger_events"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    # null only for global ops (user create/login)
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # user | aleph_agent | aiq_agent | system
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # e.g. project.create, project.update, budget.set, model_profile.set
    target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )
    prev_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # sha256(prev_chain_hash + canonical_json(payload) + target_id + action_kind + timestamp)

    __table_args__ = (
        Index("ix_ledger_project_time", "project_id", "timestamp"),
        # NO UPDATE TRIGGER: enforced at service layer + DB-level check via a trigger that
        # raises on UPDATE/DELETE.
    )
```

Migration includes a Postgres trigger that raises on UPDATE/DELETE of `action_ledger_events`:

```sql
CREATE OR REPLACE FUNCTION ledger_immutable() RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'action_ledger_events is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON action_ledger_events
  FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON action_ledger_events
  FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
```

### 0.4.5 `AgentRun` and `AgentEvent`

```python
class AgentRun(CommonColumns, Base):
    __tablename__ = "agent_runs"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # bootstrap | wiki | mechanical_reviewer | editorial_reviewer | assistant
    # | builder | aiq_orchestrator | aiq_shallow | aiq_deep | aiq_clarifier
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # pending | running | succeeded | failed | cancelled
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(4096), nullable=True)

class AgentEvent(Base):
    __tablename__ = "agent_events"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # phase_started | phase_completed | progress | blocked | error
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )
```

### 0.4.6 `ModelProfile`, `ModelBinding`, `ModelCall`

```python
class ModelProfile(CommonColumns, Base):
    __tablename__ = "model_profiles"
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # aleph-dev | aleph-production | custom names
    project_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    # null = global seeded profile; non-null = project-specific override
    is_template: Mapped[bool] = mapped_column(nullable=False, default=False)
    # seeded global profiles set is_template=True; project profiles inherit and set False
    bindings_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # serialized as dict[capability, ModelBinding-dict]

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_profile_project_name"),
    )

@dataclass
class ModelBinding:
    """In-memory shape; serialized into ModelProfile.bindings_jsonb."""
    model: str                # gateway-side model name
    provider: str             # always "litellm" in this deployment
    fallback: "ModelBinding | None" = None
    max_input_tokens: int = 200_000
    cost_per_input_token_usd: Decimal = Decimal("0")
    cost_per_output_token_usd: Decimal = Decimal("0")
    cache_discount_pct: Decimal = Decimal("0")

class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    # human-readable, e.g. "wiki.page.compose", "wiki.page_selection"
    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    cache_savings_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )
```

### 0.4.7 `CostLedgerEvent`, `Budget`

```python
class CostLedgerEvent(Base):
    """Roll-up rows written per ModelCall (1:1). Separate table from ModelCall to
    support project/agent_run rollups via materialized views in later increments.
    """
    __tablename__ = "cost_ledger_events"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    model_call_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )

class Budget(CommonColumns, Base):
    __tablename__ = "budgets"
    project_id: Mapped[UUID] = mapped_column(nullable=False, unique=True, index=True)
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    soft_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=80)
    hard_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=100)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    # maintained by trigger on cost_ledger_events insert
```

A Postgres trigger updates `budgets.spent_usd` on insert to `cost_ledger_events`:

```sql
CREATE OR REPLACE FUNCTION budget_rollup() RETURNS TRIGGER AS $$
BEGIN
  UPDATE budgets
  SET spent_usd = spent_usd + NEW.cost_usd
  WHERE project_id = NEW.project_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER cost_to_budget AFTER INSERT ON cost_ledger_events
  FOR EACH ROW EXECUTE FUNCTION budget_rollup();
```

---

## 0.5 Migrations

`apps/api/alembic/versions/<timestamp>_inc0_initial.py` creates every table above in one migration. The migration:

- Enables the `pgvector` extension (used in Inc 1; declaring now keeps a single root migration tidy)
- Creates all Inc 0 tables with their indexes
- Creates the immutability triggers on `action_ledger_events`
- Creates the budget rollup trigger
- Seeds two `ModelProfile` rows (`aleph-dev`, `aleph-production`) with `is_template=True`, `project_id=NULL`, and the binding maps from §9.1 of the top-level spec

The migration is the single source of truth — *no schema is defined outside Alembic*. The SQLAlchemy models reflect the migration; CI verifies `alembic check` is clean (autogenerate would produce no diff against the live models).

---

## 0.6 Auth + `Principal` boundary

### Contract

Every HTTP request to `aleph-api` carries a JWT bearer in `Authorization: Bearer <jwt>`. The auth middleware:

1. Validates JWT against the OIDC IdP's JWKS (cached, refreshed on `kid` miss).
2. Resolves the `sub` claim to a `User` row (JIT-provision on first sight — ledgered as `user.create`).
3. Attaches a `Principal` to `request.state.principal`.

### `Principal`

```python
# packages/aleph-security/src/aleph_security/principal.py
@dataclass(frozen=True)
class Principal:
    user_id: UUID
    subject: str
    email: str
    actor_kind: Literal["user", "aleph_agent", "aiq_agent", "system"]
    # Roles per project resolved lazily; project_scope middleware caches once
    _role_cache: dict[UUID, str] = field(default_factory=dict, compare=False)

    def role_in(self, project_id: UUID) -> str | None:
        return self._role_cache.get(project_id)
```

### `project_scope` middleware

For routes under `/projects/{project_id}/...`:

1. Looks up `ProjectMember(user_id=principal.user_id, project_id=project_id)`.
2. Caches the resulting role on `principal._role_cache`.
3. Returns 404 (not 403) if not a member — never leak existence.

### Role gates

```python
# packages/aleph-security/src/aleph_security/roles.py
class ProjectRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"

def require(principal: Principal, project_id: UUID, *, at_least: ProjectRole) -> None:
    role = principal.role_in(project_id)
    if role is None or _rank(role) < _rank(at_least):
        raise PermissionDenied()
```

### Agent tokens

Background workers (Inc 1+) and the AIQ server (Inc 3) authenticate with short-lived agent tokens scoped to one `AgentRun`. Token shape: signed JWT with `actor_kind=aleph_agent` (or `aiq_agent`), `agent_run_id`, `project_id`, `exp` ≤ 1h. Minted by `aleph-api`, never by workers. Inc 0 implements the minting endpoint (`POST /v1/agent-tokens`) and verifier; workers don't yet use it (no agents exist).

---

## 0.7 Action ledger writer service

Single entry point. Any service method that mutates state must call `ledger_writer.append(...)` *in the same transaction* as the mutation.

```python
# packages/aleph-db/src/aleph_db/repos/ledger.py
class LedgerWriter:
    def __init__(self, session: AsyncSession): ...

    async def append(
        self,
        *,
        project_id: UUID | None,
        actor: Principal,
        action_kind: str,
        target_id: UUID | None,
        target_kind: str | None,
        payload: dict,
        trace_id: str | None,
    ) -> ActionLedgerEvent:
        """Insert into action_ledger_events with hash chain.

        Race-safe via SELECT ... FOR UPDATE on the latest row with same project_id
        (or a dedicated `ledger_chain_head` row per project).
        """
        ...
```

Chain head per project tracked in a small table:

```python
class LedgerChainHead(Base):
    __tablename__ = "ledger_chain_heads"
    project_id: Mapped[UUID | None] = mapped_column(primary_key=True)
    # null = global chain
    head_event_id: Mapped[UUID | None] = mapped_column()
    head_chain_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
```

`LedgerWriter.append` locks the head row, computes the new hash, inserts the event, updates the head — all in one transaction.

### Action kinds (Inc 0)

- `user.create`
- `project.create`
- `project.update`
- `project_member.add`
- `project_member.remove`
- `project_member.role_change`
- `budget.set`
- `model_profile.create`
- `model_profile.update`
- `model_profile.copy_from_template`

Later increments add their own action kinds; all go through the same `LedgerWriter`.

---

## 0.8 LiteLLM transport client

The single chokepoint for every LLM and embedding call. **No other code in Aleph or AIQ calls a provider SDK directly.**

```python
# packages/aleph-models/src/aleph_models/client.py
class LiteLLMClient:
    """OpenAI-compatible client pointed at the Insights LiteLLM Gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        httpx_client: httpx.AsyncClient,
        tracer: Tracer,
        cost_writer: "CostWriter",
        pricing: "PricingTable",
    ): ...

    async def chat(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        capability: Capability,
        profile: ModelProfile,
        messages: list[ChatMessage],
        response_format: dict | None = None,
        tools: list[ToolSchema] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        purpose: str,
        idempotency_key: str | None = None,
    ) -> ChatResponse:
        """POST /v1/chat/completions through the gateway.

        On every call:
          1. Resolve capability → ModelBinding from profile.
          2. Start OTEL span (Langfuse-tagged).
          3. Send request with retry (tenacity exponential backoff, 3 attempts).
          4. Insert ModelCall + CostLedgerEvent in a single transaction.
          5. Return ChatResponse with prompt/completion tokens and cost echoed.
        """
        ...

    async def embed(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        profile: ModelProfile,
        input: list[str],
        purpose: str,
    ) -> EmbedResponse: ...

    async def health(self) -> bool: ...
    async def list_models(self) -> list[str]: ...
```

### Pricing

`packages/aleph-models/src/aleph_models/pricing.py` carries the per-model cost-per-token table seeded with the verified gateway models. Updated when the gateway adds new models or pricing changes. Pricing rows live in code (not DB) because they're deploy-level config — changes ship in PRs, not at runtime.

### Cost writer

`CostWriter` is a thin wrapper around `aleph-db/repos/cost.py`. Inserts `ModelCall` and `CostLedgerEvent` rows in a single SQL transaction tied to the chat/embed call. Trace ID propagates from OTEL context.

### Retry policy

`tenacity` with: 3 attempts, exponential backoff (1s, 2s, 4s), retry on `5xx`, `429`, and connection errors. Never retry on `4xx` other than `429`.

### Idempotency

For state-changing chat calls (e.g. those that produce a wiki proposal), callers may pass `idempotency_key`. The gateway is not expected to honor it; Aleph caches `(idempotency_key, model_call_id)` in Redis with 24h TTL so retries from the same key short-circuit to the recorded `ModelCall.id`.

---

## 0.9 Observability

### OTEL

`packages/aleph-observability/src/aleph_observability/tracing.py` configures:

- OTLP exporter to `OTEL_EXPORTER_OTLP_ENDPOINT`
- Resource attributes: `service.name`, `service.version`, `deployment.environment`, `aleph.profile`
- Instrumentations: FastAPI, httpx, SQLAlchemy, Arq, redis

### Langfuse

`langfuse_client.py` wraps the Langfuse SDK with:

- Auto-link to the current OTEL trace via header redaction (mirrors AIQ's `otel_header_redaction_exporter` pattern, see `~/code/aiq/src/aiq_agent/observability/`)
- Per-`AgentRun` trace creation with metadata: `project_id`, `agent_kind`, `correlation_id`, `principal.user_id`
- Span enrichment with `purpose`, `capability`, `model` for every `LiteLLMClient.chat` call

### Structlog

`logging.py` configures structlog with: JSON output, ISO-8601 timestamps, OTEL trace/span context injection, `request_id` middleware-bound, `principal.user_id` and `project_id` bound under `aleph-api` request scope.

---

## 0.10 Seeded `ModelProfile`s

The Alembic migration seeds two rows in `model_profiles` (both `is_template=True`, `project_id=NULL`):

### `aleph-dev`

```json
{
  "synthesis":      {"model": "claude-sonnet-4-6", "provider": "litellm", "max_input_tokens": 200000},
  "judge":          {"model": "claude-sonnet-4-6", "provider": "litellm", "max_input_tokens": 200000},
  "page_selection": {"model": "claude-haiku-4-5",  "provider": "litellm", "max_input_tokens": 200000},
  "extraction":     {"model": "claude-haiku-4-5",  "provider": "litellm", "max_input_tokens": 200000},
  "vision":         {"model": "claude-haiku-4-5",  "provider": "litellm", "max_input_tokens": 200000},
  "classification": {"model": "claude-haiku-4-5",  "provider": "litellm", "max_input_tokens": 200000},
  "embedding":      {"model": "cohere-embed-english-v3", "provider": "litellm"}
}
```

### `aleph-production`

```json
{
  "synthesis":      {"model": "claude-opus-4-7",   "provider": "litellm", "max_input_tokens": 200000},
  "judge":          {"model": "claude-opus-4-7",   "provider": "litellm", "max_input_tokens": 200000},
  "page_selection": {"model": "claude-sonnet-4-6", "provider": "litellm", "max_input_tokens": 200000},
  "extraction":     {"model": "claude-sonnet-4-6", "provider": "litellm", "max_input_tokens": 200000},
  "vision":         {"model": "claude-sonnet-4-6", "provider": "litellm", "max_input_tokens": 200000},
  "classification": {"model": "claude-haiku-4-5",  "provider": "litellm", "max_input_tokens": 200000},
  "embedding":      {"model": "cohere-embed-v4",   "provider": "litellm"}
}
```

`max_input_tokens` placeholder — pricing table maintained in `aleph-models/pricing.py` with actual per-model values verified against the gateway.

On `Project` creation, the API copies the template named `ALEPH_DEFAULT_MODEL_PROFILE` into a new `ModelProfile` row with `project_id` set; the new row is owned by the project and can diverge.

---

## 0.11 HTTP API (Inc 0 endpoints)

All routes under `/v1`. Auth required on every route except `/healthz` and `/readyz`. JSON request/response. Errors use RFC 7807 problem details.

### Auth

- `GET /v1/me` — returns the resolved `Principal` (user_id, email, display_name)
- `POST /v1/agent-tokens` — owner-only; mints a short-lived agent token for a given `agent_run_id`

### Projects

- `POST /v1/projects` — body `{title, description, model_profile_name, budget_usd}`; creates project, `ProjectMember` (owner), `ModelProfile` (copy of named template), `Budget`. All in one transaction. Five ledger events emitted.
- `GET /v1/projects` — list projects the principal is a member of
- `GET /v1/projects/{id}` — project detail; 404 if not member
- `PATCH /v1/projects/{id}` — owner/editor; partial update; ledgered
- `POST /v1/projects/{id}/members` — owner; add member with role
- `DELETE /v1/projects/{id}/members/{user_id}` — owner; ledgered
- `PATCH /v1/projects/{id}/members/{user_id}` — owner; role change; ledgered

### Ledger and cost

- `GET /v1/projects/{id}/ledger` — paginated; reverse chronological; query params: `since`, `until`, `actor_kind`, `action_kind`
- `GET /v1/projects/{id}/cost` — rollup: `{cap_usd, spent_usd, soft_pct, hard_pct, by_phase, by_model, recent_calls}`
- `GET /v1/projects/{id}/agent-runs` — paginated agent runs

### ModelProfile

- `GET /v1/model-profile-templates` — list global templates
- `GET /v1/projects/{id}/model-profile` — current project profile
- `PATCH /v1/projects/{id}/model-profile` — owner; update bindings; ledgered

### Health

- `GET /healthz` — liveness; returns 200 if process is up
- `GET /readyz` — readiness; checks Postgres, Redis, MinIO, LiteLLM gateway (`GET /v1/models`); returns 503 if any are down with details

### Smoke

- `POST /v1/projects/{id}/smoke/llm` — owner-only; calls `LiteLLMClient.chat` with a hardcoded "ping" prompt; returns the response, model, tokens, cost; **every call is traced and ledgered**. This is the acceptance test path; CI hits it.

---

## 0.12 Worker shell

`apps/workers` runs Arq with the Redis broker. One demonstration job:

```python
# apps/workers/src/aleph_workers/jobs/smoketest.py
async def smoke_llm_job(
    ctx: dict,
    project_id_str: str,
    agent_token: str,
) -> dict:
    """Background round-trip through the gateway. Authenticates back to aleph-api
    with the agent token (Inc 0 just demonstrates the auth path — no agent yet)."""
    principal = await resolve_agent_token(agent_token)
    project_id = UUID(project_id_str)
    response = await ctx["litellm_client"].chat(
        principal=principal,
        project_id=project_id,
        agent_run_id=None,
        capability="classification",
        profile=await get_project_profile(project_id),
        messages=[ChatMessage(role="user", content="ping")],
        max_tokens=16,
        purpose="inc0.smoke",
    )
    return {"ok": True, "model": response.model, "content": response.choices[0].message.content}
```

CI does not need workers to verify the smoke — the API smoke endpoint hits the gateway synchronously. Workers are demonstrated separately by a manual `arq aleph_workers.arq.WorkerSettings` run.

---

## 0.13 Frontend shell

`apps/web` is intentionally minimal in Inc 0. The three-panel shell exists with empty surfaces.

### Routes (Tanstack Router or similar)

- `/` — login redirect
- `/projects` — project list
- `/projects/$id` — workspace (empty surfaces; placeholder text "Wiki surface will appear here")
- `/projects/$id/settings` — minimal (model profile picker, budget)

### Components (Inc 0 scope)

- `AppShell` — top bar, left panel, center panel, right panel
- `LeftPanel` — project switcher + sessions list (sessions = empty for now) + bottom icon row (gear, logs, notifications, profile)
- `CenterPanel` — chat placeholder + activity card placeholder
- `RightPanel` — 5 tab buttons (Wiki, Artifacts, Notes, Hypotheses, Briefs) + empty surface area
- `ProjectListPage`, `ProjectCreateModal` (form: title, description, model profile picker, budget)
- `CostBanner` — top-right, fetches `/v1/projects/{id}/cost`

A2UI renderer is **not** wired in Inc 0 (lands in Inc 4). Right-panel surfaces are placeholder React components.

### Auth flow

OIDC code+PKCE flow against the configured IdP. Token storage: `sessionStorage` only (no `localStorage`). Refresh handled by the IdP's standard flow.

---

## 0.14 Tests

### Unit (in each package)

- `aleph-core`: ID generation, time utilities, schemas (round-trip JSON)
- `aleph-db`: every model creates+reads via a `pytest-asyncio` fixture using a throwaway DB
- `aleph-security`: JWT verify (mocked JWKS), Principal resolution, role gates
- `aleph-models`: LiteLLM client retry policy (httpx mock), profile resolver, pricing
- `aleph-observability`: OTEL span emission (capture exporter)

### Integration (`tests/e2e/`)

- `test_project_lifecycle.py` — create → fetch → update → add member → remove member → archive. Every step verifies a ledger event exists with the expected `action_kind` and chain hash continues correctly.
- `test_ledger_immutable.py` — attempt UPDATE and DELETE; expect Postgres trigger to raise.
- `test_smoketest_llm.py` — call `POST /v1/projects/{id}/smoke/llm`; verify response shape; verify a `ModelCall` row and `CostLedgerEvent` row were inserted; verify a Langfuse span was emitted (test against a Langfuse fixture, not the production instance).
- `test_budget_enforcement.py` — set a tiny `cap_usd`, fire enough smoke calls to cross soft and then hard; verify soft response includes a banner header and hard returns `429` with a clear problem-detail body.
- `test_permission_leakage.py` — Member A in Project X cannot read Project Y's ledger/cost/profile. Must return 404, not 403.
- `test_gateway_unreachable.py` — when LiteLLM gateway is down, `/readyz` returns 503 with details and project creation still succeeds (gateway is not required for project create).

### Eval skeleton

`packages/aleph-evals` ships an empty `runner.py` that:

- Discovers eval datasets from a directory (none in Inc 0; populated in Inc 8)
- Iterates and writes results to a JSON report
- Returns nonzero exit code if any gate fails

CI calls `python -m aleph_evals.runner --datasets all --gate strict`. In Inc 0, this is a no-op that passes by default.

---

## 0.15 CI

`.github/workflows/ci.yml` runs on every push and PR:

1. `pnpm install` + `uv sync`
2. `ruff check` + `ruff format --check`
3. `pyright` (or `mypy --strict`)
4. `pnpm typecheck` (`tsc --noEmit`)
5. `pnpm lint` (ESLint)
6. `alembic check` — autogenerate would produce no diff
7. `pytest -m "not integration"` — unit tests
8. Bring up compose stack via `scripts/bootstrap-local.sh`; run `pytest -m integration` (e2e tests above)
9. `python -m aleph_evals.runner --datasets all --gate strict`
10. Build frontend (`pnpm build`); upload as artifact

PR fails any step → no merge.

---

## 0.16 Local bootstrap

`scripts/bootstrap-local.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# 1. Verify .env exists
[ -f deploy/compose/.env ] || { echo "Copy deploy/compose/.env.example to .env"; exit 1; }

# 2. Bring up infra services
docker compose -f deploy/compose/docker-compose.yml up -d postgres minio redis langfuse otel-collector

# 3. Wait for Postgres
until docker compose -f deploy/compose/docker-compose.yml exec -T postgres pg_isready -U aleph; do sleep 1; done

# 4. Run migrations
(cd apps/api && uv run alembic upgrade head)

# 5. Initialize MinIO bucket
docker compose -f deploy/compose/docker-compose.yml exec -T minio mc alias set local http://localhost:9000 aleph changeme-local
docker compose -f deploy/compose/docker-compose.yml exec -T minio mc mb local/aleph-local --ignore-existing

# 6. Verify LiteLLM gateway
scripts/verify-gateway.sh

# 7. Start api + workers + web
docker compose -f deploy/compose/docker-compose.yml up -d aleph-api aleph-workers aleph-web

echo "✓ Aleph local stack up at http://localhost:5173 (web), http://localhost:8000 (api)"
```

`scripts/verify-gateway.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
source deploy/compose/.env

curl -sS --max-time 10 -H "Authorization: Bearer $INSIGHTS_LITELLM_API_KEY" \
  "$LITELLM_BASE_URL/v1/models" > /tmp/aleph-models.json

python3 -c "
import json, sys
required = {
  'claude-opus-4-7', 'claude-sonnet-4-6', 'claude-haiku-4-5',
  'cohere-embed-v4', 'cohere-embed-english-v3',
}
got = {m['id'] for m in json.load(open('/tmp/aleph-models.json'))['data']}
missing = required - got
if missing:
  print(f'✗ LiteLLM gateway missing required models: {sorted(missing)}', file=sys.stderr)
  sys.exit(1)
print(f'✓ gateway has {len(got)} models; all required present')
"
```

---

## 0.17 Documentation deliverables

Files created in this increment:

- `docs/engineering/local-development.md` — how to run the stack
- `docs/engineering/repo-structure.md` — package layout, conventions
- `docs/engineering/quality-gates.md` — what CI enforces and why
- `docs/engineering/litellm-transport.md` — how to use `LiteLLMClient`, what it traces/ledgers/costs
- `docs/domain/ledger.md` — action ledger schema, hash chain, action_kind taxonomy (starting with Inc 0's kinds)
- `docs/security/auth.md` — JWT verify flow, Principal, agent tokens, role gates
- `docs/operations/runbook.md` — start/stop, common diagnostics, gateway-down playbook, ledger inspection queries
- `docs/implementation-log.md` — Inc 0 entry written at the end (template below)

### Implementation log template (every increment uses this)

```markdown
## Increment N: <name>

**Completed:** YYYY-MM-DD
**Commit range:** <first sha>..<last sha>

### What was built
- ...

### Key files changed
- ...

### Migrations added
- <revision> — <one-line summary>

### Tests added
- unit: <count> across packages
- integration: <count> in tests/e2e
- eval: <added/none>

### Trace and ledger behavior added
- Ledger action kinds: ...
- New OTEL spans: ...
- Cost ledger coverage: ...

### Manual verification
- [ ] `scripts/bootstrap-local.sh` succeeds
- [ ] CI green on a fresh clone
- [ ] <increment-specific>

### Known issues
- ...

### Next increment entry point
- See `docs/superpowers/specs/2026-05-27-inc-<N+1>-<name>-design.md`
```

---

## 0.18 Acceptance criteria

Increment 0 is **done** when all of these hold (the coding agent verifies each before closing the increment):

1. **One-command boot.** `scripts/bootstrap-local.sh` brings up the stack on a fresh clone with no manual steps beyond `cp .env.example .env` and filling secrets. Idempotent on re-run.
2. **Migrations apply cleanly.** `alembic upgrade head` on an empty DB succeeds; `alembic check` produces no diff against models.
3. **Web app loads.** `http://localhost:5173` renders the 3-panel shell. Login button visible; after OIDC login, project list page shown.
4. **Project create works end-to-end.** POST `/v1/projects` creates project + member + profile + budget; 5 ledger events written with chain hash continuity; UI updates.
5. **Smoke LLM call works.** `POST /v1/projects/{id}/smoke/llm` returns a chat completion via the gateway. A `ModelCall` and `CostLedgerEvent` row are inserted. The Langfuse trace for the call appears in the local Langfuse UI with the project_id and purpose attributes.
6. **Ledger is immutable.** `UPDATE action_ledger_events SET payload_jsonb=...` raises. `DELETE FROM action_ledger_events` raises.
7. **Budget enforcement.** Setting `cap_usd=0.01` and firing smoke calls returns soft-cap banner at 80%, hard-cap 429 at 100%. Both states are observable in `/v1/projects/{id}/cost`.
8. **Permission leakage zero.** Member A cannot read any of Member B's project's resources (ledger, cost, profile). Returns 404 with no leaked existence signal.
9. **Gateway down handling.** With the gateway unreachable, `/readyz` returns 503 listing `litellm_gateway` as down. Project create still succeeds. Smoke endpoint returns a clear problem-detail with `gateway_unavailable` error code.
10. **CI green.** All steps in `.github/workflows/ci.yml` pass on a fresh PR.
11. **Docs complete.** All files listed in §0.17 exist and reflect the built system.
12. **Implementation log written.** `docs/implementation-log.md` has the Inc 0 entry filled out.
13. **No placeholders.** Grep for `TODO|FIXME|XXX|NotImplementedError|pass  #` in production paths returns zero. Tests-only stubs allowed.

---

## 0.19 Handoff to Increment 1

Increment 1 depends on Inc 0 providing:

- The `LedgerWriter` (used by every Inc 1 service)
- The `LiteLLMClient` with `embedding` capability resolved (used by chunk embedding worker)
- The `Project` and `ModelProfile` (every wiki row carries `project_id`)
- The Arq worker shell (Inc 1 normalization + embedding jobs run here)
- The OTEL + Langfuse trace context (every wiki agent step is traced)
- The CI eval-runner skeleton (Inc 1 introduces the first eval cases)
- The `agent_tokens` mechanism (the wiki agent uses one)

Increment 1's first task is to add new entities (`Source`, `NormalizedDocument`, `DocumentChunk`, `SourcePage`, `WikiPage`, …) via a new Alembic migration, never by editing Inc 0's migration.

See `docs/superpowers/specs/2026-05-27-inc-1-rks-wiki-skeleton-design.md`.
