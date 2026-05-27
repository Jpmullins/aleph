# Quality gates

CI runs every gate listed below. A failure on any gate blocks merge.

## CI pipeline (`.github/workflows/ci.yml`)

1. **`pnpm install` + `uv sync`** — deterministic dependency resolution.
2. **`ruff check`** — lint Python.
3. **`ruff format --check`** — formatting.
4. **`pyright`** — strict type-check Python.
5. **`pnpm typecheck`** — `tsc --noEmit` for the web app.
6. **`pnpm lint`** — ESLint for the web app.
7. **`alembic upgrade head`** — migrations apply on a fresh CI DB.
8. **`alembic check`** — autogenerate would produce zero diff against models.
9. **Unit tests** — `pytest -m "not integration"`.
10. **Integration tests** — `pytest -m integration` against a CI Postgres + Redis.
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
