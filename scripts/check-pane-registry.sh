#!/usr/bin/env bash
#
# The client and the server must agree on what a pane is.
#
# They did not. `routes/surfaces.py` accepted seven pane kinds; the client's
# `SURFACE_TABS` was a five-element constant. So `artifacts` and `grounding`
# could be streamed by the backend and had nowhere to land — and
# `GroundingSurface` shipped complete on every layer (React impl, catalog entry,
# component api, server builder, route branch) with no code path able to open
# it. Nothing failed. The pane simply did not exist as far as the UI knew.
#
# Neither side can detect this alone: the server drops unknown tabs rather than
# raising, precisely so one bad pane in a URL cannot take down the workspace
# stream. That kindness is what let the drift live.
#
# Compares the `wire` values in `apps/web/src/lib/workspace-ui.tsx`'s
# PANE_REGISTRY against `_PANE_KINDS` in `apps/api/.../routes/surfaces.py`.
#
# CI-wired. Fails on: a pane either side knows about and the other does not.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import pathlib
import re
import sys

client_path = pathlib.Path("apps/web/src/lib/workspace-ui.tsx")
server_path = pathlib.Path("apps/api/src/aleph_api/routes/surfaces.py")

for p in (client_path, server_path):
    if not p.is_file():
        print(f"✗ missing {p}", file=sys.stderr)
        raise SystemExit(1)

client_src = client_path.read_text()
block = re.search(r"export const PANE_REGISTRY = \{(.*?)\n\} as const", client_src, re.S)
if block is None:
    print("✗ PANE_REGISTRY not found — did the registry move?", file=sys.stderr)
    raise SystemExit(1)
client = set(re.findall(r'wire:\s*"([^"]+)"', block.group(1)))

server_src = server_path.read_text()
sblock = re.search(r"_PANE_KINDS\s*=\s*frozenset\(\s*\{(.*?)\}\s*\)", server_src, re.S)
if sblock is None:
    print("✗ _PANE_KINDS not found — did the server registry move?", file=sys.stderr)
    raise SystemExit(1)
server = set(re.findall(r'"([^"]+)"', sblock.group(1)))

if not client or not server:
    print(f"✗ parsed an empty set (client={client}, server={server})", file=sys.stderr)
    raise SystemExit(1)

client_only = sorted(client - server)
server_only = sorted(server - client)

if client_only or server_only:
    print("✗ pane kinds disagree between client and server", file=sys.stderr)
    for k in client_only:
        print(f"    client knows {k!r}; the server will silently DROP it", file=sys.stderr)
    for k in server_only:
        print(f"    server accepts {k!r}; the client can never open it", file=sys.stderr)
    raise SystemExit(1)

print(f"✓ pane registry: {len(client)} kinds, client and server agree")
PY
