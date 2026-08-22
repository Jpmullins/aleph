"""No job talks to a gateway it did not resolve for a project. WS-MEP-4 c6.

The criterion as the plan first wrote it was a grep for `app.state.litellm`,
and a grep is the wrong instrument here twice over: it counts the comments that
explain the fix (the API half went from 3 hits to 6 by being *repaired*), and
it names a symbol the worker process does not have. What has to be true on this
side is a property of the source, so it is checked as one.

Three halves, because two of them are the same claim seen from opposite
directions and the third is the door somebody walks through next year:

* **The old seam is gone.** `ctx["litellm_client"]` and `ctx["gateway_catalog"]`
  were one client and one catalog built at boot from `LITELLM_BASE_URL`, so a
  project's `gateway_endpoints` row reached the settings screen and none of its
  background traffic. Subscripts, matched in the syntax tree, so the prose above
  cannot satisfy the check that the prose describes.
* **The new seam is there.** Absence of the wrong call is not presence of the
  right one — a job could simply stop making model calls, or make them through
  something else, and pass the first check. Every module that spends on a model
  is named here and must resolve through `gateways(ctx)`.
* **Nobody opens a private door.** A `LiteLLMClient` constructed inside a job
  would take its base URL from wherever that line felt like reading it, which is
  the same defect with a new spelling and no ctx key to grep for.

`tests/integration/test_worker_gateway_endpoints.py` is the behavioural half:
two real fakes, request counters, and a project repointed mid-process. This
file is the sweep that stops the twelfth job from being written the old way,
and it is deliberately not a substitute for that one.
"""

from __future__ import annotations

import ast
from pathlib import Path

WORKERS_SRC = Path(__file__).resolve().parents[1] / "src" / "aleph_workers"
JOBS_DIR = WORKERS_SRC / "jobs"

#: The `ctx` keys that used to hold a process-wide model client and catalog.
FORBIDDEN_CTX_KEYS = ("litellm_client", "gateway_catalog")

#: Every job module that makes, or hands somebody else the means to make, a
#: billed model call. Written out rather than inferred: a heuristic for "does
#: this module spend money" is exactly the kind of check that quietly stops
#: matching, and this list going stale is a failure the third test catches.
SPENDING_JOBS = (
    "autoconfigure.py",
    "backfill_index.py",
    "bootstrap.py",
    "chunk_embed.py",
    "curate.py",
    "reembed.py",
    "research.py",
    "reviewers.py",
    "smoketest.py",
    "wiki_ingest.py",
    "wiki_refresh.py",
)


def _worker_modules() -> list[Path]:
    return sorted(p for p in WORKERS_SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _subscript_string_keys(tree: ast.AST) -> list[tuple[int, str]]:
    """Every `something["literal"]` in the module, with its line."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            value = node.slice.value
            if isinstance(value, str):
                out.append((node.lineno, value))
    return out


def test_the_source_this_test_scans_is_really_there() -> None:
    """A sweep over an empty file list passes for the wrong reason.

    Every assertion below is `offenders == []`, and a glob that matched nothing
    would satisfy all of them while checking nothing at all — one of the six
    cannot-fail checks this repository has already shipped.
    """
    modules = _worker_modules()
    assert len(modules) > 10, f"only {len(modules)} worker modules found under {WORKERS_SRC}"
    found = {p.name for p in JOBS_DIR.glob("*.py")}
    missing = [name for name in SPENDING_JOBS if name not in found]
    assert missing == [], f"SPENDING_JOBS names modules that do not exist: {missing}"


def test_no_worker_module_reads_a_process_wide_model_client() -> None:
    """The defect, as a tree shape rather than as a string.

    Matched on the subscript so this file's own docstring — which spells both
    keys out — cannot make the check pass or fail. A grep could not tell the
    difference, and the API-side criterion was misled by exactly that.
    """
    offenders: list[str] = []
    for path in _worker_modules():
        rel = path.relative_to(WORKERS_SRC)
        for lineno, key in _subscript_string_keys(_tree(path)):
            if key in FORBIDDEN_CTX_KEYS:
                offenders.append(f"{rel}:{lineno} ctx[{key!r}]")
    assert offenders == [], (
        "a worker read a model client built once at boot from LITELLM_BASE_URL; "
        "resolve it per project with aleph_workers.gateway.gateways(ctx) so a "
        f"project's gateway_endpoints row decides where its traffic goes. {offenders}"
    )


def test_every_spending_job_resolves_its_gateway_for_a_project() -> None:
    """The positive half. Not spending is not the same as spending correctly."""
    missing: list[str] = []
    for name in SPENDING_JOBS:
        tree = _tree(JOBS_DIR / name)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "gateways"
        ]
        if not calls:
            missing.append(f"{name}: no gateways(ctx) call")
            continue
        # And it has to be given something — `gateways()` with no argument
        # cannot be reading the job context.
        if not any(call.args for call in calls):
            missing.append(f"{name}: gateways() called with no context")
    assert missing == [], (
        "these jobs make billed model calls without resolving which gateway the "
        f"project they are running for uses: {missing}"
    )


def test_no_job_constructs_its_own_model_client() -> None:
    """The door that has no ctx key to grep for.

    `LiteLLMClient(base_url=settings.litellm_base_url, ...)` inside a job is the
    same defect wearing a constructor. The registry in
    `aleph_models.endpoints` is the only place one is built for a worker, so
    two projects on one endpoint keep sharing a pool and a ceiling.
    """
    offenders: list[str] = []
    for path in _worker_modules():
        rel = path.relative_to(WORKERS_SRC)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "LiteLLMClient"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "a worker built its own LiteLLMClient; go through "
        f"aleph_workers.gateway.WorkerGateways instead. {offenders}"
    )


def test_the_startup_publishes_the_resolver_the_jobs_read() -> None:
    """The two ends of the ctx key, checked against each other.

    `arq.py` writing `ctx["gateways"]` and the jobs reading `ctx["resolver"]`
    would leave every test above green and every job raising at runtime — the
    drift `test_background_task_kinds.py` exists to catch for the other pair of
    halves this process holds.
    """
    from aleph_workers.gateway import GATEWAYS_KEY

    tree = _tree(WORKERS_SRC / "arq.py")
    written = {
        key
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript)
        for _line, key in _subscript_string_keys(target)
    }
    # `arq.py` assigns through the imported constant, so the literal will not
    # appear as an Assign target — the check is that the assignment EXISTS and
    # goes through the constant.
    #
    # Not `"GATEWAYS_KEY" in source`: that is satisfied by the import line,
    # which `_shutdown` needs anyway. Measured — changing
    # `ctx[GATEWAYS_KEY] = WorkerGateways(...)` to `ctx["gateway_resolver"] =
    # WorkerGateways(...)` left 21 worker tests, 6 integration tests and ruff
    # green, while all eleven jobs would raise on their first run. That is
    # verbatim the drift this test's own docstring claims to catch, and the
    # string form did not catch it.
    published = {
        (target.value.id, target.slice.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and isinstance(target.slice, ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "WorkerGateways"
    }
    assert ("ctx", "GATEWAYS_KEY") in published, (
        "arq.py does not assign `ctx[GATEWAYS_KEY] = WorkerGateways(...)`. Every "
        "job reads GATEWAYS_KEY, so publishing under any other key — or not "
        f"publishing at all — makes all eleven raise on first run. Found: {published or 'nothing'}"
    )
    assert GATEWAYS_KEY not in written, (
        "arq.py hardcodes the ctx key as a literal; import GATEWAYS_KEY so the "
        "two ends cannot drift"
    )


def test_no_job_reaches_past_the_resolver_to_the_boot_settings() -> None:
    """The deployment default may be read by the resolver, and by nothing else.

    `WorkerGateways.resolve` falls back to `settings.litellm_base_url` when a
    project has no `gateway_endpoints` row — that is the whole point of a
    fallback. Any OTHER reader of that setting is a project-scoped call quietly
    addressed to the deployment default, which is the exact defect MEP-4
    exists to remove and which the API route had first.

    Nothing caught it: reverting `autoconfigure_profile_job`'s
    `base_url=resolved.base_url` to `ctx["settings"].litellm_base_url` left 21
    worker tests and 6 integration tests green, because the sweep above only
    asserts `gateways(ctx)` is CALLED — and it still is. Found by an
    adversarial pass.
    """
    allowed = {"gateway.py", "settings.py"}
    offenders: list[str] = []
    for path in sorted(WORKERS_SRC.rglob("*.py")):
        if path.name in allowed or "__pycache__" in path.parts:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr in {
                "litellm_base_url",
                "insights_litellm_api_key",
            }:
                offenders.append(f"{path.relative_to(WORKERS_SRC)}:{node.lineno} .{node.attr}")

    assert offenders == [], (
        "a worker reads the deployment's gateway settings directly instead of "
        "resolving the project's endpoint. Only WorkerGateways may read them, as "
        "the no-row fallback: " + "; ".join(offenders)
    )
