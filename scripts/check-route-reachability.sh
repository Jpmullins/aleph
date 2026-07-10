#!/usr/bin/env bash
#
# WP-5 item 11 — route-reachability sweep (Final State F5 item 1).
#
# Enumerates every `app.include_router(<mod>.router)` mount in
# `apps/api/src/aleph_api/main.py`, reads each router's concrete route paths,
# and asserts each mounted router is REACHED by at least one real caller — i.e.
# one of its route URLs appears in the reachability corpus:
#
#   * apps/web/src              — browser fetch / EventSource / apiUrl
#   * apps/api/src (non-routes) — in-process self-calls (copilot_agent,
#     subagents/*, a2ui_handlers `/cards/actions` handlers), mint_agent_token
#     sites, cross-route calls
#   * apps/workers/src, packages — worker callbacks into the API
#   * scripts/                  — external operator scripts (connect-consensus.py)
#   * tests/                    — the integration/e2e/playwright suites that
#     exercise a route's contract (a route with an exercised contract is not
#     "silently dead")
#
# A route matches even when the caller templates the ids (`${projectId}` in TS,
# `{project_id}` in Python f-strings): path params are turned into a
# no-slash wildcard.
#
# The router's OWN route file is excluded from its corpus, so a router's own
# decorators never self-satisfy the check.
#
# Fails on a mounted router reached by nobody. The commented allowlist below
# documents every intentionally-external / public exception. The catalog
# producer/renderer sweep (`check-catalog-roster.sh`) already exists from WP-4.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run python - "$ROOT" <<'PY'
import importlib
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
main_py = (root / "apps/api/src/aleph_api/main.py").read_text()

# ---------------------------------------------------------------------------
# Allowlist — intentionally-external / public routers with no in-repo caller.
# Each entry is documented; the sweep skips reachability for these.
# ---------------------------------------------------------------------------
ALLOWLIST = {
    # Public, auth-bypassed liveness/readiness + docs. No app caller by design.
    "health",
    # External token-mint boundary: external agents/workers POST /v1/agent-tokens
    # to obtain a scoped agent token. In-process code calls mint_agent_token()
    # directly, so nothing in-repo hits the ROUTE — it exists for outside callers.
    "agent_tokens",
    # Sound, unit-tested push endpoint (ChangeBroker SSE + `changes` serializers,
    # tests/.../test_changes_serializers.py) retained as the data source for the
    # planned wiki-build/refresh progress signals. Its previous frontend consumer
    # (the useWikiLiveSignals hook) was wired to stale query keys and was removed;
    # the replacement consumer is roadmapped in docs/future-work.md.
    "changes",
}

# Router modules, in mount order, from main.py's include_router(...) calls.
mods: list[str] = re.findall(r"app\.include_router\((\w+)\.router\)", main_py)

# ---------------------------------------------------------------------------
# Reachability corpus: every place a real caller of a route may live.
# ---------------------------------------------------------------------------
CORPUS_DIRS = [
    "apps/web/src",
    "apps/api/src",
    "apps/workers/src",
    "packages",
    "scripts",
    "tests",
]
EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh"}
ROUTES_DIR = root / "apps/api/src/aleph_api/routes"

corpus: dict[str, str] = {}
for d in CORPUS_DIRS:
    base = root / d
    if not base.exists():
        continue
    for f in base.rglob("*"):
        if f.suffix in EXTS and f.is_file():
            try:
                corpus[str(f)] = f.read_text(errors="ignore")
            except OSError:
                pass


def path_to_regex(path: str) -> re.Pattern[str]:
    esc = re.escape(path)
    # re.escape turns "{param}" into "\{param\}"; make each a no-slash wildcard
    # so caller templates ($ {projectId} / {project_id}) still match.
    esc = re.sub(r"\\\{[^}]*\\\}", lambda _m: r"[^/\s'\"`)]+", esc)
    return re.compile(esc)


fail = 0
reached = 0
for mod in mods:
    module = importlib.import_module(f"aleph_api.routes.{mod}")
    paths = sorted(
        {getattr(rt, "path", "") for rt in module.router.routes if getattr(rt, "path", "")}
    )
    if mod in ALLOWLIST:
        print(f"~ {mod:<22} allowlisted (external/public) — {len(paths)} route(s)")
        continue

    own_file = str(ROUTES_DIR / f"{mod}.py")
    hit = None
    for p in paths:
        rx = path_to_regex(p)
        for fpath, text in corpus.items():
            if fpath == own_file:
                continue
            if rx.search(text):
                rel = fpath[len(str(root)) + 1 :]
                hit = (p, rel)
                break
        if hit:
            break

    if hit:
        reached += 1
        print(f"✓ {mod:<22} {hit[0]}  <-  {hit[1]}")
    else:
        fail = 1
        print(f"✗ {mod:<22} UNREACHED — no web/agent/script/test caller for any of:")
        for p in paths:
            print(f"      {p}")

print("")
if fail:
    print(
        "Route-reachability sweep FAILED — a mounted router is reached by nobody.\n"
        "Delete the dead route (route file + main.py include + routes/__init__.py\n"
        "entry, keeping the ORM tables) or add a documented allowlist entry."
    )
    sys.exit(1)

print(
    f"✓ route reachability: {reached} routers reached by a real caller, "
    f"{len(ALLOWLIST)} allowlisted (external/public)"
)
PY
