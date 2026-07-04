# aleph-code-runner — the isolated sandbox worker (WP-4 sub-spec c).
#
# Minimal on purpose: a FIXED, audited scientific stack and NOTHING else. No
# aleph packages, no database drivers, no S3 client, no secrets. The image is
# built from a standalone pinned pip install (NOT `uv sync --all-packages`), so
# nothing from the workspace but this app's own source lands in the image.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/scratch/mpl

WORKDIR /app

# The audited stack, pinned. arq (+ redis) is the only transport; the rest is
# pure in-process computation the sandboxed subprocess may use.
RUN pip install --no-cache-dir \
      "arq==0.28.0" \
      "matplotlib==3.11.0" \
      "numpy==2.4.6" \
      "pandas==2.3.3" \
      "altair==5.5.0"

# Only this app's source — no other workspace member.
COPY apps/code-runner/src /app/src
ENV PYTHONPATH=/app/src

# Non-root. The compose service additionally sets cap_drop:[ALL], read_only
# rootfs + tmpfs /scratch, pids_limit, tight mem_limit, and a Redis-only
# internal network.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin coderunner
USER 10001

CMD ["arq", "aleph_code_runner.arq.WorkerSettings"]
