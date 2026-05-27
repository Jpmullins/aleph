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
COPY packages /app/packages
COPY apps/api /app/apps/api
COPY apps/workers /app/apps/workers

RUN uv sync --no-dev --frozen || uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "aleph_api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
