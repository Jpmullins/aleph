# Quality gates

CI runs every gate listed below. A failure on any gate blocks merge.

**Status (2026-05-29): all five CI jobs are green on `main`** — `lint-and-typecheck`,
`unit-tests`, `integration-tests`, `evals`, `build-web`. (They had been red for a
while; see the green-the-gates pass and the CI-pipeline fix in `implementation-log.md`.)

## CI pipeline (`.github/workflows/ci.yml`)

1. **`pnpm install` + `uv sync --all-packages --all-extras`** — deterministic
   dependency resolution. The `--all-packages` flag is **required**: `uv sync
   --all-extras` alone does not install the workspace members, so `pyright` then
   fails with `reportMissingImports`. Every job uses the full flag.
2. **`ruff check`** — lint Python.
3. **`ruff format --check`** — formatting.
4. **`pyright`** — strict type-check Python.
5. **`pnpm typecheck`** — `tsc --noEmit` for the web app.
6. **`pnpm lint`** — ESLint for the web app (ESLint 9 flat config at `apps/web/eslint.config.js`).
7. **`alembic upgrade head`** — migrations apply on a fresh CI DB.
8. **`alembic check`** — autogenerate would produce zero diff against models.
9. **Unit tests** — `pytest -m "not integration"` (97 tests).
10. **Integration tests** — `pytest -m integration` (14 tests) against CI Postgres +
    Redis **+ MinIO**. MinIO is started as a `docker run` step (GitHub `services:` can't
    pass MinIO's `server /data` command); the `aleph-local` bucket auto-creates on first
    use by the lifespan's `AssetStore`. Without MinIO, the ingest/upload routes 422
    ("asset store is not configured"). `ALEPH_ENV` must be `local` (the `Settings`
    `Literal`; `ci` is rejected). The permission-leakage test builds its own oidc-mode
    app — it is vacuous under local auth mode (one dev principal for all requests).
11. **Eval suite** — `python -m aleph_evals --gate strict`.
12. **Web build** — `pnpm -C apps/web build`.

## What "strict mode" means in evals

Inc 0 ships the runner skeleton with no datasets. `--gate strict` exits 0
when no datasets exist. Inc 8 adds:

- Permission leakage check (must be zero)
- Wiki coverage threshold (per `ModelProfile`)
- Citation correctness threshold
- Cost drift detection (warn at 15%)

## Hard rules enforced by gates

- **No placeholder code in production paths.** A grep gate in CI checks
  for `TODO|FIXME|NotImplementedError` outside tests.
- **No ledger gap on state mutations.** Integration tests for every
  increment include a ledger-event-count assertion per mutation.
- **No cost gap.** Integration tests assert `ModelCall` + `CostLedgerEvent`
  rows for every smoke / chat / embed call.
- **No silent failures.** Every error path returns an explicit RFC 7807
  problem document.
- **No new global resources.** All entities are project-scoped (or `ModelProfile`
  template, which is the one exception).

## Local pre-push

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright \
  && uv run pytest -m "not integration" -q \
  && pnpm -C apps/web typecheck \
  && pnpm -C apps/web build
```
