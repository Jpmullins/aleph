"""E2 — eight concurrent scholar searches finish, and none of them 503s.

The reported symptom was "literature search fails with 'service unavailable'",
and the fan-out E5 in docs/backlog.md calls "weirdly rate limited" is the same
token bucket: it ran at 1 req/s with a burst of 5, so the sixth concurrent
search waited a second, the seventh two, and each of them then had three retry
attempts of its own to get through. The research loop's `search` phase fans out
exactly like this.

Two things make this probe mean something rather than merely pass:

1. **The eight queries are DISTINCT.** Identical queries now collapse into one
   upstream request by design (single-flight de-duplication), so eight copies
   of one query would prove nothing about throughput at all — it would measure
   the de-duplication and report it as a rate-limit result.
2. **A 503 is a failure, not a slow success.** The point of the workstream is
   that a 503 on this endpoint used to mean six different things. Any 503 here
   fails the probe and prints the body, which now carries a reason.

It talks to the real API against the real upstreams, because the risk the
change carries — a rate limit set too high getting the deployment's mailto
blocked — cannot be observed against a stub.

Exit codes: 0 pass, 1 fail, 2 precondition missing (no API, no project) so the
caller can report SKIP rather than FAIL.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get("ALEPH_API_URL", "http://localhost:8000").rstrip("/")
PROJECT_ID = os.environ.get("ALEPH_PROJECT_ID", "")
FANOUT = 8
BUDGET_S = 30.0
#: One search per sub-question, the way the research loop's `search` phase
#: fans out. Distinct on purpose — see the module docstring.
QUERIES = [
    "graph neural network expressivity",
    "retrieval augmented generation evaluation",
    "sparse autoencoder interpretability",
    "constitutional AI preference modelling",
    "speculative decoding latency",
    "mixture of experts routing collapse",
    "long context attention sinks",
    "reward model overoptimisation",
]


def _request(path: str, payload: dict[str, object] | None = None, timeout: float = BUDGET_S):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # connection refused, DNS, read timeout
        return None, repr(exc)


def _resolve_project() -> str | None:
    if PROJECT_ID:
        return PROJECT_ID
    status, body = _request("/v1/projects", timeout=10.0)
    if status != 200:
        return None
    try:
        projects = json.loads(body)
    except ValueError:
        return None
    return str(projects[0]["id"]) if projects else None


def main() -> int:
    status, _ = _request("/healthz", timeout=5.0)
    if status != 200:
        print(f"SKIP: no API at {API}")
        return 2

    project_id = _resolve_project()
    if project_id is None:
        print("SKIP: no project to search in (set ALEPH_PROJECT_ID)")
        return 2

    path = f"/v1/projects/{project_id}/scholar/search"

    def one(query: str) -> tuple[str, int | None, str, float]:
        started = time.monotonic()
        code, body = _request(
            path, {"provider": "openalex", "query": query, "limit": 5, "deadline_s": 20.0}
        )
        return query, code, body, time.monotonic() - started

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=FANOUT) as pool:
        results = list(pool.map(one, QUERIES[:FANOUT]))
    wall = time.monotonic() - started

    ok = 0
    for query, code, body, took in results:
        verdict = "ok " if code == 200 else "BAD"
        print(f"  {verdict} {code!s:>5}  {took:5.1f}s  {query}")
        if code == 200:
            ok += 1
        else:
            print(f"        {body[:400]}")

    print(f"{ok}/{FANOUT} returned 200 in {wall:.1f}s wall clock (budget {BUDGET_S:.0f}s)")
    if wall > BUDGET_S:
        print(f"FAIL: the fan-out took {wall:.1f}s, over the {BUDGET_S:.0f}s budget")
        return 1
    if ok < FANOUT - 1:
        print(f"FAIL: only {ok}/{FANOUT} succeeded (at most one failure is tolerated)")
        return 1
    if any(code == 503 for _, code, _, _ in results):
        print("FAIL: a 503 on this endpoint is the defect E2 exists to remove")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
