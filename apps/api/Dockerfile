# Pinned by digest, not by tag. `python:3.13-slim` is a moving pointer: two
# builds a fortnight apart shipped different interpreters, different OpenSSL and
# different Debian packages with nothing in the diff to say so, and a CVE scan of
# "the image we build" named an image nobody could reproduce. The tag is kept in
# the line so a human can still read which base this is; the digest is what
# docker resolves. `.github/dependabot.yml`'s docker ecosystem is what moves it
# forward, and `.github/workflows/security.yml` scans exactly this digest.
# Resolved 2026-08-21 with `docker buildx imagetools inspect python:3.13-slim`
# — the multi-arch INDEX digest, so it resolves on linux/amd64 and linux/arm64
# alike; a per-platform manifest digest would break every non-amd64 build.
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
    libpango-1.0-0 libpangoft2-1.0-0 libjpeg62-turbo fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
# ^ WeasyPrint runtime libs (aleph-artifacts is in the API's dep closure).

# Pinned by digest for the same reason as the base: `uv:0.5.21` is a tag, and a
# retagged 0.5.21 would silently change the binary that resolves every Python
# dependency in this image. Resolved 2026-08-21 with
# `docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.5.21`.
COPY --from=ghcr.io/astral-sh/uv:0.5.21@sha256:87d729f94c87d0aade077260d07d899848f027b7ef37e734dca711c7ceffd3cf /uv /usr/local/bin/uv

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
