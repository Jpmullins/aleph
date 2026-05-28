# Repository Guidelines

## Project Structure & Module Organization

Aleph is a monorepo with a Python `uv` workspace and one `pnpm` package. Runtime apps live in `apps/`: `apps/api` is FastAPI and owns Alembic migrations, `apps/workers` contains Arq jobs, and `apps/web` is the React/Vite UI. Shared Python packages live under `packages/aleph-*`, each using `src/` layout and local `tests/` where applicable. Cross-package tests are in `tests/e2e`; docs and specs are in `docs/`; local infrastructure is under `deploy/compose`.

## Build, Test, and Development Commands

- `uv sync --all-extras`: install Python dependencies.
- `pnpm -C apps/web install`: install web dependencies.
- `./scripts/bootstrap-local.sh`: create local config and boot compose services.
- `uv run ruff check .` / `uv run ruff format --check .`: lint and check Python formatting.
- `uv run pyright`: run strict Python type checks.
- `pnpm -C apps/web typecheck`, `pnpm -C apps/web lint`, `pnpm -C apps/web build`: validate the web app.
- `uv run pytest -m "not integration" -q`: run unit tests.
- `uv run pytest -m integration -q`: run integration tests with local services.
- `cd apps/api && uv run alembic upgrade head && uv run alembic check`: apply migrations and verify drift.

## Coding Style & Naming Conventions

Use spaces, LF endings, final newlines, and trimmed trailing whitespace per `.editorconfig`: 4 spaces for Python, 2 for TS/JSON/YAML/Markdown/CSS. Ruff targets Python 3.13, 100-column lines, double quotes, and import sorting. Pyright runs in strict mode. Distribution names use `aleph-xxx`; Python modules use `aleph_xxx`; database tables are plural `snake_case`; action kinds follow `<entity>.<verb>`.

## Testing Guidelines

Pytest discovers `tests`, `packages`, and `apps`. Mark service-dependent tests with `@pytest.mark.integration`; keep default tests runnable with `uv run pytest -m "not integration" -q`. Add package-local tests beside changed code, and use `tests/e2e` for cross-package behavior such as permissions, ledger integrity, and project lifecycle.

## Commit & Pull Request Guidelines

Recent commits use concise, scope-first subjects such as `Inc 8: Eval suite + UserFeedback + regression gates` or `Add specs README index`. Keep PRs focused. Include a short description, linked issue or spec, migration notes, screenshots for UI changes, and exact verification commands. New schema changes need a new Alembic revision; do not edit existing migrations.

## Agent-Specific Instructions

Respect the package DAG: apps depend on packages, packages do not depend on apps, and `aleph-core` remains the leaf. Route LLM calls through `aleph-models`/LiteLLM and state mutations through services that write ledger events in the same transaction.
