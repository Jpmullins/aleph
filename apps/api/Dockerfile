FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

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

CMD ["uv", "run", "uvicorn", "aleph_api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
