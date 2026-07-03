FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
    libpango-1.0-0 libpangoft2-1.0-0 libjpeg62-turbo fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
# ^ WeasyPrint runtime libs (aleph-artifacts is in the API's dep closure).

COPY --from=ghcr.io/astral-sh/uv:0.5.21 /uv /usr/local/bin/uv

COPY pyproject.toml ./pyproject.toml
COPY uv.lock ./uv.lock
COPY packages /app/packages
COPY apps/api /app/apps/api
COPY apps/workers /app/apps/workers

# --all-packages installs every workspace member (alembic, uvicorn, all
# the aleph-* libraries). The root pyproject has no direct deps.
RUN uv sync --no-dev --all-packages

EXPOSE 8000

# Invoke the venv binary directly: the container may run as an arbitrary uid
# (compose sets user: for the shared asset bind mount), and `uv run` would
# need a writable cache/HOME that uid doesn't have.
CMD ["/app/.venv/bin/uvicorn", "aleph_api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
