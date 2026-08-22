#!/usr/bin/env bash
#
# The deploy layer is the only place some invariants can be stated, and every
# one of them is a single line that is easy to drop and impossible to notice.
#
# What went wrong without this sweep:
#
#   * NO SERVICE DECLARED A RESTART POLICY. The only `restart:` key in the file
#     was `restart: "no"` on the one-shot `migrate`. A crash, an OOM kill or a
#     host reboot left the whole stack down until a person noticed — and the way
#     a person notices is a browser tab that does not load.
#   * NO SERVICE BOUNDED ITS LOGS. The json-file driver's default is unbounded,
#     so the disk fills and the symptom is "postgres won't start".
#   * ONE SERVICE HAD A MEMORY CAP, and it was the sandbox — not the API, which
#     is the process that reads an upload into RAM before it reaches the store.
#   * `LITELLM_BASE_URL` REACHED THE CONTAINERS ONLY THROUGH `env_file:`.
#     Compose uses an env_file value as-is: a shell variable of the same name is
#     neither interpolated into the file nor injected into the container. So
#     `LITELLM_BASE_URL=http://127.0.0.1:1 docker compose up -d`, the command
#     that is supposed to prove the stack survives a dead gateway, quietly ran
#     against the WORKING gateway from .env and proved nothing.
#
# A grep in a doc cannot hold any of these: it reads the source text, not the
# rendered configuration, so it cannot see a value that arrives through a YAML
# merge key and cannot tell an anchor definition from a service that uses it.
# SCOPE, because it is narrower than it looks: this renders from a scratch copy
# of `.env.example`, so it measures WHAT A FRESH OPERATOR GETS from the shipped
# defaults. It does not read your machine's `deploy/compose/.env`, and it does
# not read the RUNNING container — a `.env` setting `POSTGRES_SHM_SIZE=64m`
# leaves this green while giving that operator an unrestorable database.
# That live path is acceptance row P8a's job: `restore_drill.py` performs a
# real pg_restore against the real server, which is where a 64 MB /dev/shm
# actually surfaces. The two rows are complementary and neither subsumes the
# other; do not widen this one into a live probe.
#
# This sweep parses `docker compose config` — the merged, interpolated result
# that the daemon is actually given.
#
# It renders in a SCRATCH DIRECTORY with `.env.example` copied to `.env`, which
# makes the sweep hermetic (CI has no hand-written .env) and asserts something
# worth asserting on its own: the shipped example file is sufficient to render
# the stack. A required key added to the compose file and forgotten in the
# example fails here rather than on a new machine.
#
# Exit codes: 0 all sections pass · 1 a section failed · 2 the sweep could not
# run (no docker). 2 is distinct on purpose — "could not measure" must never be
# reported the same way as "measured, and it was fine".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="deploy/compose/docker-compose.yml"
ENV_EXAMPLE="deploy/compose/.env.example"

for f in "$COMPOSE" "$ENV_EXAMPLE"; do
  if [[ ! -f "$f" ]]; then
    echo "✗ MISSING: $f — this sweep names a file that does not exist" >&2
    exit 1
  fi
done

if ! docker compose version >/dev/null 2>&1; then
  echo "⚠ SKIP: \`docker compose\` is not available, so nothing was measured." >&2
  echo "  This is NOT a pass. Exit 2." >&2
  exit 2
fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# Everything the compose file mounts from its own directory has to come along,
# or rendering fails on a missing bind-mount source rather than on anything this
# sweep is about.
cp "$COMPOSE" "$SCRATCH/docker-compose.yml"
cp deploy/compose/*.yaml "$SCRATCH/" 2>/dev/null || true
cp "$ENV_EXAMPLE" "$SCRATCH/.env"

render() {
  # $1 is an optional LITELLM_BASE_URL override, exercising interpolation.
  local override="${1:-}"
  if [[ -n "$override" ]]; then
    LITELLM_BASE_URL="$override" docker compose \
      -f "$SCRATCH/docker-compose.yml" --profile tracing --profile s3 config
  else
    docker compose -f "$SCRATCH/docker-compose.yml" --profile tracing --profile s3 config
  fi
}

if ! render > "$SCRATCH/rendered.yml" 2> "$SCRATCH/render.err"; then
  echo "✗ the compose file does not render from .env.example alone:" >&2
  sed 's/^/    /' "$SCRATCH/render.err" >&2
  exit 1
fi
render "http://127.0.0.1:1" > "$SCRATCH/rendered-override.yml" 2>/dev/null

uv run --quiet python - "$SCRATCH/rendered.yml" "$SCRATCH/rendered-override.yml" <<'PY'
import pathlib
import sys

import yaml

rendered, overridden = (yaml.safe_load(pathlib.Path(p).read_text()) for p in sys.argv[1:3])
services: dict[str, dict[str, object]] = rendered["services"]

failures: list[str] = []


def fail(msg: str, *detail: str) -> None:
    failures.append(msg)
    print(f"✗ {msg}", file=sys.stderr)
    for line in detail:
        print(f"    {line}", file=sys.stderr)


# --- 1. restart policy ------------------------------------------------------
#
# Classified by name rather than counted, so ADDING a service forces a decision
# here instead of quietly landing with no policy under a `>= 8` threshold that
# the other eight already satisfy.
ONE_SHOT = {"migrate", "langfuse-db", "minio-buckets"}
managed = sorted(set(services) - ONE_SHOT)

for name in managed:
    got = services[name].get("restart")
    if got != "unless-stopped":
        fail(
            f"{name}: restart={got!r}, expected 'unless-stopped'",
            "a crash or a host reboot leaves this service down until a human notices",
        )
for name in sorted(ONE_SHOT & set(services)):
    got = services[name].get("restart")
    if got not in ("no", False):
        fail(
            f"{name}: restart={got!r}, expected 'no' — it is a one-shot",
            "a restart policy here retries a failing job forever while every",
            "dependent waits on an exit code that never comes",
        )
if not failures:
    print(f"✓ restart policy: {len(managed)} long-running services `unless-stopped`, "
          f"{len(ONE_SHOT & set(services))} one-shots `no`")

# --- 2. log rotation --------------------------------------------------------
before = len(failures)
for name in sorted(services):
    logging = services[name].get("logging") or {}
    assert isinstance(logging, dict)
    options = logging.get("options") or {}
    assert isinstance(options, dict)
    if not options.get("max-size") or not options.get("max-file"):
        fail(
            f"{name}: no bounded log rotation (max-size/max-file)",
            "the json-file driver defaults to UNBOUNDED; this container can fill the host disk",
        )
if len(failures) == before:
    print(f"✓ log rotation: all {len(services)} services bounded by max-size and max-file")

# --- 3. memory caps ---------------------------------------------------------
#
# `mem_limit` alone caps memory and lets the container swap instead, which turns
# a fast failure into a host-wide stall. Equal `memswap_limit` is the only way
# compose can say "no swap" — `deploy.resources` cannot express it at all.
MEM_CAPPED = {"api", "workers", "web", "copilot-runtime", "code-runner"}
before = len(failures)
for name in sorted(MEM_CAPPED):
    if name not in services:
        fail(f"{name}: named in MEM_CAPPED but not in the compose file")
        continue
    mem = services[name].get("mem_limit")
    swap = services[name].get("memswap_limit")
    if not mem:
        fail(
            f"{name}: no mem_limit",
            "unbounded, this process can take the host down by accident",
        )
    elif mem != swap:
        fail(
            f"{name}: mem_limit={mem} but memswap_limit={swap}",
            "unequal means the container swaps at the limit rather than failing",
        )
if len(failures) == before:
    print(f"✓ memory caps: {len(MEM_CAPPED)} services capped with swap disabled")

# --- 3b. postgres has more than the default /dev/shm -------------------------
#
# Docker's default is a 64 MB tmpfs at /dev/shm, and Postgres puts the shared
# memory a PARALLEL worker needs there. On 2026-08-22 that default made this
# instance's own backup unrestorable: `scripts/_acceptance/restore_drill.py`
# ran `scripts/backup.sh` and `scripts/restore.sh` against the live database
# and pg_restore died on
#
#   CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks USING hnsw (...)
#     ERROR: could not resize shared memory segment to 64000896 bytes:
#            No space left on device
#
# with 40 GB free on the host. "No space left on device" naming a 61 MB
# allocation on an empty disk is the single most misread Postgres-in-Docker
# error there is, which is why the floor is asserted here rather than left to
# whoever next tries a restore at 3am.
#
# A FLOOR, not an equality: raising the cap is always safe (it is a ceiling on a
# tmpfs, not an allocation), lowering it back to the default is the regression.
# 256 MiB is the floor rather than 1 GiB so a smaller deployment can size it
# down deliberately; the compose default is 1g.
SHM_FLOOR = 256 * 1024 * 1024
before = len(failures)
raw_shm = services.get("postgres", {}).get("shm_size")
if raw_shm is None:
    fail(
        "postgres: no shm_size, so it gets Docker's 64 MB default",
        "a parallel index build — including the one every restore performs —",
        "fails with 'No space left on device' while the disk is nearly empty",
    )
else:
    try:
        shm = int(raw_shm)
    except (TypeError, ValueError):
        shm = -1
        fail(f"postgres: shm_size={raw_shm!r} did not render as a byte count")
    if 0 <= shm < SHM_FLOOR:
        fail(
            f"postgres: shm_size={shm} is below the {SHM_FLOOR}-byte floor",
            "a parallel index build needs more shared memory than this and fails",
            "with 'No space left on device' on an empty disk",
        )
if len(failures) == before:
    print(f"✓ shared memory: postgres /dev/shm is {int(raw_shm) // (1024 * 1024)} MiB, "
          f"above the {SHM_FLOOR // (1024 * 1024)} MiB floor a parallel index build needs")

# --- 4. the gateway URL is interpolatable -----------------------------------
#
# The behavioural form of the check, not a grep for the key. Compose merges
# `env_file` into `environment` when it renders, so the rendered output cannot
# tell you WHERE a value came from — but it can tell you whether a shell
# variable wins, which is the only property that matters.
before = len(failures)
for name in ("api", "workers"):
    got = overridden["services"][name].get("environment", {}).get("LITELLM_BASE_URL")
    if got != "http://127.0.0.1:1":
        fail(
            f"{name}: LITELLM_BASE_URL is not interpolatable (rendered {got!r})",
            "a shell override does not reach the container, so the 'stack boots with no",
            "gateway' test silently runs against the working gateway from .env",
        )
if len(failures) == before:
    print("✓ gateway URL: a shell LITELLM_BASE_URL override reaches api and workers")

# --- 5. no image runs as root ------------------------------------------------
#
# DELIBERATELY RED TODAY, and this section is the reason to say so out loud: 4
# of the 5 Dockerfiles this stack builds declare no USER, so they run as uid 0
# with the workspace source and (for `api`/`workers`) the assets volume mounted.
# The fix is one line per file plus ownership of /app/data/assets set at build
# time — the named volume is chown'd from the image on first creation, so adding
# USER without it breaks the asset store instead of securing it.
before = len(failures)
# The union of "what compose builds" and "every Dockerfile under apps/", so a
# production image that the dev stack does not run — apps/web/Dockerfile — is
# held to the same rule instead of escaping the sweep by not being referenced.
dockerfiles = sorted(
    {
        str(spec["dockerfile"])
        for spec in (s.get("build") for s in services.values())
        if isinstance(spec, dict) and spec.get("dockerfile")
    }
    | {str(p) for p in pathlib.Path("apps").glob("*/Dockerfile*")}
)
if not dockerfiles:
    fail("no Dockerfiles found in the rendered compose file — the sweep is looking in the wrong place")
for rel in dockerfiles:
    path = pathlib.Path(rel)
    if not path.is_file():
        fail(f"{rel}: named by a compose build stanza and does not exist")
        continue
    lines = path.read_text(encoding="utf-8").splitlines()
    users = [ln for ln in lines if ln.startswith("USER ")]
    if not users:
        fail(
            f"{rel}: no USER — this image runs as root",
            "add a non-root uid, and for images that mount `assets:` chown the mount",
            "point at build time or the named volume comes up owned by root",
        )
    elif users[-1].split()[1] in ("root", "0"):
        fail(f"{rel}: last USER is {users[-1].split()[1]!r} — this image runs as root")
if len(failures) == before:
    print(f"✓ non-root: all {len(dockerfiles)} built images declare a non-root USER")

if failures:
    print(f"\n{len(failures)} compose-hardening failure(s).", file=sys.stderr)
    raise SystemExit(1)
print(f"\n✓ compose hardening: {len(services)} services, {len(dockerfiles)} images, all sections pass")
PY
