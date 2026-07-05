#!/usr/bin/env bash
#
# WP-7 — CLAUDE.md fresh-clone executability guard (Final State F6 item 3).
#
# Extracts the shell commands from CLAUDE.md's ```bash command blocks and
# asserts every referenced script / file / directory and every named
# docker-compose service actually exists in the repo — so a command in
# CLAUDE.md can't reference a deleted or renamed thing on a fresh clone.
# It does NOT run docker; it only checks that referents exist.
#
# It also scans the whole file for banned "deleted things" (nvcr.io, NGC_API_KEY,
# aiq-server, the AIQ jobs, the aiq :8001 endpoint) and fails on any hit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run python - "$ROOT" <<'PY'
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
claude = (root / "CLAUDE.md").read_text()

fail = []

# --- 1. Banned deleted-things anywhere in the file --------------------------
BANNED = [
    "nvcr.io",
    "NGC_API_KEY",
    "aiq-server",
    "aiq_synthesis_poll_job",
    "deep_researcher",
    ":8001",
]
for tok in BANNED:
    if tok in claude:
        fail.append(f"banned reference to a deleted thing: '{tok}'")

# --- 2. Collect the ```bash command blocks ----------------------------------
blocks = re.findall(r"```bash\n(.*?)```", claude, re.DOTALL)
cmd_text = "\n".join(blocks)

# --- 3. Compose service names -----------------------------------------------
compose = (root / "deploy/compose/docker-compose.yml").read_text()
# service names: 2-space-indented keys under the top-level `services:` map.
services = set()
in_services = False
for line in compose.splitlines():
    if line.rstrip() == "services:":
        in_services = True
        continue
    if in_services:
        if line and not line[0].isspace():  # dedent to a new top-level key
            in_services = False
            continue
        m = re.match(r"^  ([a-z0-9][a-z0-9-]*):\s*$", line)
        if m:
            services.add(m.group(1))

# `docker compose ... logs -f <svc>` (and similar) must name a real service.
for svc in re.findall(r"logs\s+-f\s+([a-z0-9-]+)", cmd_text):
    if svc not in services:
        fail.append(f"compose service '{svc}' referenced in CLAUDE.md does not exist")

# --- 4. Referenced paths (scripts / files / dirs) must exist ----------------
PATH_RE = re.compile(r"(?:\./)?(?:\.github|scripts|apps|packages|deploy|docs|tests|ci)/[\w./-]+")

def is_placeholder(tok: str) -> bool:
    if "path/to" in tok or "::" in tok or "<" in tok or ">" in tok or "*" in tok:
        return True
    if re.search(r"YYYY|MMDD|<slug>|<message>|test_file|test_name", tok):
        return True
    # `cp .env.example .env` — the .env target is generated, not committed.
    if pathlib.PurePath(tok).name == ".env":
        return True
    return False

checked = 0
for raw in PATH_RE.findall(cmd_text):
    tok = raw.lstrip("./").rstrip(".,);:`'\"")
    if not tok or is_placeholder(tok):
        continue
    checked += 1
    if not (root / tok).exists():
        fail.append(f"path referenced in a CLAUDE.md command does not exist: '{tok}'")

# --- 5. The scripts CLAUDE.md names as living invariants must exist ----------
for s in re.findall(r"scripts/[\w-]+\.sh", claude):
    if not (root / s).exists():
        fail.append(f"script referenced in CLAUDE.md does not exist: '{s}'")

if fail:
    print("✗ CLAUDE.md command/reference check FAILED:")
    for f in sorted(set(fail)):
        print(f"    {f}")
    sys.exit(1)

print(
    f"✓ CLAUDE.md: {checked} command-block path refs exist, "
    f"compose services + invariant scripts resolve, no banned deleted-things"
)
PY
