FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
    libpango-1.0-0 libpangoft2-1.0-0 libjpeg62-turbo fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
# ^ WeasyPrint runtime libs (Builder PDF export). libpango pulls glib/gobject/
#   cairo/harfbuzz transitively; without these `import weasyprint` raises OSError
#   on libgobject-2.0-0. (Inc-7 PDF export was never exercised in-container.)

COPY --from=ghcr.io/astral-sh/uv:0.5.21 /uv /usr/local/bin/uv

COPY pyproject.toml ./pyproject.toml
COPY uv.lock ./uv.lock
COPY packages /app/packages
COPY apps/api /app/apps/api
COPY apps/workers /app/apps/workers

# --all-packages installs every workspace member (and their deps,
# including arq's CLI). The root pyproject has no direct dependencies.
RUN uv sync --no-dev --all-packages

CMD ["uv", "run", "arq", "aleph_workers.arq.WorkerSettings"]
