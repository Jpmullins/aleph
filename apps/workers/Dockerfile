# Pinned by digest, not by tag. `python:3.13-slim` is a moving pointer: two
# builds a fortnight apart shipped different interpreters, different OpenSSL and
# different Debian packages with nothing in the diff to say so, and a CVE scan of
# "the image we build" named an image nobody could reproduce. The tag is kept in
# the line so a human can read which base this is; the digest is what docker
# resolves. `.github/dependabot.yml`'s docker ecosystem is what moves it forward,
# and `.github/workflows/security.yml` scans exactly this digest.
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
    libxcb1 libgl1 \
 && rm -rf /var/lib/apt/lists/*
# ^ WeasyPrint runtime libs (Builder PDF export). libpango pulls glib/gobject/
#   cairo/harfbuzz transitively; without these `import weasyprint` raises OSError
#   on libgobject-2.0-0. (Inc-7 PDF export was never exercised in-container.)
#
#   `libxcb1` and `libgl1` are docling's, via `docling_ibm_models` → `cv2`. The
#   resolved wheel is `opencv-python`, not `opencv-python-headless`, so its
#   `cv2.abi3.so` links `libxcb.so.1` and `libGL.so.1` — X11 and OpenGL, in a
#   worker that draws nothing. Without them the TableFormer stage raises
#   `ImportError: libxcb.so.1` at CONVERT time, `DoclingNormalizer` turns it
#   into `NormalizationFailed`, and `normalize_bytes` falls back to pdfminer —
#   so the symptom is not an error, it is every PDF quietly coming out flat with
#   `parser="pdfminer"`. Measured in the built image before this line, on a real
#   paper: `parser=pdfminer  heading_count=0  21/40 chunks unsectioned`.
#   Found with `ldd cv2.abi3.so | grep 'not found'`, which names exactly these
#   two and nothing else.

# Pinned by digest for the same reason as the base, and it matters more here:
# this binary resolves every Python dependency in the image. `uv:0.5.21` is a
# tag, so a retagged 0.5.21 would silently change the resolver with the base
# still looking reproducible. Resolved 2026-08-21 with
# `docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.5.21`.
COPY --from=ghcr.io/astral-sh/uv:0.5.21@sha256:87d729f94c87d0aade077260d07d899848f027b7ef37e734dca711c7ceffd3cf /uv /usr/local/bin/uv

COPY pyproject.toml ./pyproject.toml
COPY uv.lock ./uv.lock
COPY packages /app/packages
COPY apps/api /app/apps/api
COPY apps/workers /app/apps/workers

# --all-packages installs every workspace member (and their deps,
# including arq's CLI). The root pyproject has no direct dependencies.
#
# `--extra pdf-layout` is what makes PDF ingest structure-aware — WS-RS11.
# Without it `pdf_normalizer()` falls through to pypdf, which extracts the words
# and none of the structure, and `chunk_markdown` (which finds a section by
# looking for a markdown `#`) then writes `section_path = NULL` on every passage
# of every PDF. Measured on this instance before the extra was installed:
# **0.905 of pypdf chunks had no section path, and no document had ever been
# parsed by docling** — the layout normalizer had been chosen on numbers
# (`docs/measurements/pdf-parsers.md`), shipped, and left out of the image that
# runs it. Measured on one real 14-page paper, host-side, through the production
# `pdf_normalizer` and the production chunker:
#
#     docling   30 headings · 14 table rows · 0/58 chunks without a section path
#     pypdf      0 headings ·  0 table rows · 51/51 chunks without a section path
#
# It is an EXTRA and not a dependency for the reason `aleph-rks/pyproject.toml`
# gives: it pulls torch and roughly a gigabyte of model stack. Installed HERE
# and not in `apps/api/Dockerfile`, because `normalize_bytes` has exactly two
# callers and both are worker jobs — the API never parses a PDF, so making it
# carry the model stack would be a gigabyte for nothing.
#
# The cache mount is what keeps that affordable to rebuild. Without it every
# rebuild re-downloads ~1.5 GB of wheels because the `COPY packages` above
# invalidates this layer on any source change.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --all-packages --extra pdf-layout

# Layout and table weights, baked rather than fetched at ingest time.
#
# docling downloads these from HuggingFace on its FIRST conversion. That turns
# the first PDF a fresh worker touches into a job that stalls on a ~500 MB
# download inside an arq task, and a deployment with no egress to
# huggingface.co into one where PDF ingest fails at runtime having reported
# nothing at build time. Both paths are pointed at the baked copy:
# `DOCLING_ARTIFACTS_PATH` for docling's own resolver and `HF_HOME` for the
# hub cache underneath it.
ENV HF_HOME=/app/.cache/huggingface \
    DOCLING_ARTIFACTS_PATH=/app/.cache/docling/models
RUN --mount=type=cache,target=/root/.cache/uv \
    /app/.venv/bin/docling-tools models download -o /app/.cache/docling/models \
      layout tableformer

# Without this, the FIRST PDF kills the interpreter.
#
#   Inconsistency detected by ld.so: dl-tls.c: 1193: _dl_add_to_slotinfo:
#   Assertion `idx == 0' failed!
#
# Measured in this image on linux/arm64: torch and its OpenMP/BLAS libraries are
# dlopen'd after startup and exhaust glibc's static TLS surplus, and glibc
# aborts the process rather than raising anything Python can catch. It is not a
# Python exception, so `except NormalizationFailed` cannot see it and the arq
# worker does not record a failure — the job simply dies, which is the same
# shape of silence WS-RS1 spent a workstream removing. Raising the surplus is
# the documented workaround and costs a couple of megabytes of address space.
ENV GLIBC_TUNABLES=glibc.rtld.optional_static_tls=2097152

# tiktoken, offline.
#
# `NormalizationResult.token_count` calls `tiktoken.get_encoding("cl100k_base")`,
# which downloads the BPE table from openaipublic.blob.core.windows.net on first
# use and caches it under TMPDIR. Every normalization computes `token_count`, so
# a worker with no egress to that host fails on its first document — and a
# worker WITH egress silently depends on a Microsoft blob store being up in
# order to ingest a PDF. Baked and pinned to a fixed cache directory.
ENV TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken
RUN /app/.venv/bin/python -c \
    "import tiktoken; tiktoken.get_encoding('cl100k_base').encode('warm')"


# Run as an unprivileged uid.
#
# `data/assets` has to exist and be owned by that uid BEFORE the volume is
# attached: Docker seeds a NEW named volume's ownership from whatever the image
# has at the mount point, so creating it here is what makes the volume writable.
# An existing volume from a previous root-owned run is NOT re-chowned by this —
# see `deploy/README.md` for the one-line fix.
#
# `/app/.cache` goes with it. The baked docling weights are only READ at
# inference, but `huggingface_hub` takes a lock file beside the snapshot before
# it will hand one back, and a root-owned cache under an unprivileged uid turns
# that into a PermissionError on the first PDF rather than on the first build.
RUN mkdir -p /app/data/assets \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin aleph \
 && chown -R 10001:10001 /app/data /app/.cache
USER 10001

# Invoke the venv binary directly: this image runs as an unprivileged uid and
# `uv run` would want a writable cache/HOME it does not have.
#
# The previous version of this comment said "compose sets user: for the shared
# asset bind mount". Compose sets no `user:` anywhere — checked — and `assets`
# is a named volume, not a bind mount. The reasoning was right and both of its
# stated facts were wrong, which is how the image ended up running as root
# while a comment implied somebody had thought about it.
CMD ["/app/.venv/bin/arq", "aleph_workers.arq.WorkerSettings"]
