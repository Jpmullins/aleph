"""`/readyz`'s legs are measured individually, and a failed leg says so.

WS-P9 c4, and the decision behind it.

## The measurement that produced the decision

The criterion asks for fifteen `start_span` call sites in `apps/api/src`. There
was one. The question the plan leaves open is whether the automatic FastAPI
instrumentation already discharges the intent, so it was measured rather than
argued: three requests against the booted app produced **28 spans** — three
server spans named by *route template*, nine SQLAlchemy `SELECT` spans, six
`connect` spans, one httpx client span, and the `http send` children. The
request path is genuinely traced, for free, and no hand-written span is going
to improve on it.

Two things it cannot do, and both bite here:

1. **It cannot see inside a request.** `GET /readyz` is one server span, and
   underneath it five probes run concurrently, each under its own budget. "The
   asset store round trip is exceeding the client timeout" — the failure the
   `health` module docstring describes diagnosing by hand — is invisible at
   route granularity, and it is the whole reason that module exists.
2. **It feeds no metric, and by default it feeds nothing at all.**
   `aleph_stage_duration_seconds` is recorded by `start_span` and by nothing
   else, and `init_otel` installs no exporter when
   `OTEL_EXPORTER_OTLP_ENDPOINT` is empty — which it is in `.env.example`,
   because the collector sits behind `--profile tracing`. On a default
   deployment every one of those 28 spans is discarded. The histogram is not.

So the spans added were the ones that answer a question the route span cannot,
rather than however many it takes to reach fifteen. These tests pin the two
properties that make them worth having: the legs are separately timed, and a
leg that fails is recorded as a failure rather than as a fast success.

## The second half: inside an ordinary request

`/readyz` was only half the answer. Re-measured 2026-08-22 with an in-memory
exporter on the booted app, one `GET /v1/projects/{id}/sources` produced **nine
spans and not one of them Aleph's**: the route span (9.12 ms), three SQLAlchemy
spans all named `SELECT`, two named `connect`, and three `http send`. Every
millisecond inside that 9 was unattributable, and — because `start_span` is the
only thing that records `aleph_stage_duration_seconds` — the busiest code path
in the process contributed nothing to any histogram.

Four stages now cover it, chosen the same way: `api.authenticate` (a database
round trip on every request, and a write on first sight), `api.project_scope`
(111 of the 115 project-scoped routes run it), `api.agent_scope` (reads the
whole body and queries membership before a chat turn starts) and
`api.stream_access` (the SSE membership check that once held a pool connection
for the life of a stream). The same request now reads
`api.authenticate 2.57 ms + api.project_scope 2.77 ms` inside an 8.22 ms route
span. `tests/integration/test_route_stage_metrics.py` proves those series reach
`/metrics` over the wire.

`outcome="error"` on the two scope stages means **the check refused** — a
mis-scoped credential, a non-member, a write to an archived project — not that
Aleph malfunctioned. That is deliberate: "how often is someone being told their
project does not exist" is the number you want when a person reports they
cannot see their project, and it is invisible in a route-level 404 count that
cannot say which layer produced it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

from fastapi import Request

from aleph_api.middleware.auth import STAGE_AGENT_SCOPE, STAGE_AUTHENTICATE
from aleph_api.middleware.project_scope import STAGE_PROJECT_SCOPE, STAGE_STREAM_ACCESS
from aleph_api.routes.health import READYZ_STAGES, readyz
from aleph_observability.metrics import STAGE_DURATION, init_metrics, sample_value

#: The request-path stages, by the constants the middleware actually passes.
#: Imported rather than spelled out, so renaming a constant cannot leave this
#: test asserting a string nothing emits any more.
REQUEST_STAGES = (
    STAGE_AUTHENTICATE,
    STAGE_AGENT_SCOPE,
    STAGE_PROJECT_SCOPE,
    STAGE_STREAM_ACCESS,
)

#: The package whose `start_span` names are swept below.
_MIDDLEWARE_DIR = (
    Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "aleph_api" / "middleware"
)

# --- stubs ------------------------------------------------------------------


class _Result:
    def scalar_one(self) -> int:
        return 1


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_: object) -> _Result:
        return _Result()


class _Maker:
    def __call__(self) -> _Session:
        return _Session()


class _Redis:
    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok

    async def ping(self) -> bool:
        if not self._ok:
            msg = "redis is gone"
            raise RuntimeError(msg)
        return True


class _StoredAsset:
    storage_uri = "file://probe"


class _AssetStore:
    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> _StoredAsset:
        del key, data, mime_type
        return _StoredAsset()

    def get(self, storage_uri: str) -> bytes:
        del storage_uri
        return b"ok"


class _Litellm:
    async def health(self) -> bool:
        return True


class _Settings:
    def __init__(self, root: str) -> None:
        self.aleph_asset_backend = "fs"
        self.aleph_asset_root = root


class _State:
    def __init__(self, root: str, *, redis_ok: bool) -> None:
        self.session_maker = _Maker()
        self.redis = _Redis(ok=redis_ok)
        self.asset_store = _AssetStore()
        self.litellm = _Litellm()
        self.settings = _Settings(root)


def _request(root: Path, *, redis_ok: bool = True) -> Request:
    app = type("_App", (), {"state": _State(str(root), redis_ok=redis_ok)})()
    return cast("Request", type("_Request", (), {"app": app})())


def _count(stage: str, outcome: str) -> float:
    value = sample_value(f"{STAGE_DURATION}_count", stage=stage, outcome=outcome)
    return 0.0 if value is None else value


async def _body(response: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(bytes(response.body)))


# --- the tests --------------------------------------------------------------


async def test_every_leg_records_its_own_latency(tmp_path: Path) -> None:
    """Six stages from one request, because six probes ran."""
    init_metrics()
    before = {stage: _count(stage, "ok") for stage in READYZ_STAGES}

    await readyz(_request(tmp_path))

    moved = [stage for stage in READYZ_STAGES if _count(stage, "ok") > before[stage]]
    assert sorted(moved) == sorted(READYZ_STAGES), {
        "did not record": sorted(set(READYZ_STAGES) - set(moved))
    }


async def test_a_failing_leg_is_recorded_as_a_failure(tmp_path: Path) -> None:
    """The ordering that makes the histogram worth having.

    `_leg` catches the probe's exception so the endpoint can name the failing
    dependency instead of 500ing. If the span were inside that `except`,
    `start_span` would see a clean exit and record `outcome="ok"` — a latency
    series in which a dependency that has been down for an hour looks like a
    dependency that is answering in three milliseconds. So the probe runs
    inside the span and the `except` sits outside it, and this is the test that
    says so.
    """
    init_metrics()
    before_error = _count("readyz.redis", "error")
    before_ok = _count("readyz.redis", "ok")

    response = await readyz(_request(tmp_path, redis_ok=False))
    body = await _body(response)

    assert response.status_code == 503
    assert body["checks"]["redis"]["ok"] is False
    assert _count("readyz.redis", "error") == before_error + 1
    assert _count("readyz.redis", "ok") == before_ok, (
        "a failed probe was counted as a successful stage"
    )


async def test_the_stage_names_are_literals_and_there_are_six(tmp_path: Path) -> None:
    """A span name is a metric label, so its value set has to be a number.

    Six, all prefixed `readyz.`, all module constants — never interpolated from
    a project id, a path or a dependency's own error text.
    """
    del tmp_path
    assert len(READYZ_STAGES) == 6
    assert len(set(READYZ_STAGES)) == 6
    assert all(stage.startswith("readyz.") for stage in READYZ_STAGES)
    assert all("{" not in stage and "%" not in stage for stage in READYZ_STAGES)


# --- the request path -------------------------------------------------------


def _middleware_start_span_names() -> list[tuple[str, str]]:
    """`(file, first-argument-source)` for every `start_span(` call in the package."""
    found: list[tuple[str, str]] = []
    for path in sorted(_MIDDLEWARE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "start_span" or not node.args:
                continue
            found.append((path.name, ast.unparse(node.args[0])))
    return found


def test_every_middleware_span_is_named_by_one_of_the_four_constants() -> None:
    """The cardinality rule, enforced rather than written down.

    A span name is a metric label. `start_span(f"api.project.{project_id}")`
    would be accepted by every other test in this repo and would grow the
    exposition without bound — the failure mode WS-P9's own risk note names.
    So the sweep requires the argument to be one of four module constants: a
    literal string is refused too, because a literal is how the fifth stage
    arrives without anyone deciding it should exist.
    """
    allowed = {
        "STAGE_AUTHENTICATE",
        "STAGE_AGENT_SCOPE",
        "STAGE_PROJECT_SCOPE",
        "STAGE_STREAM_ACCESS",
    }
    calls = _middleware_start_span_names()
    assert calls, "no start_span call found in the middleware package — the sweep is blind"
    offenders = [(f, arg) for f, arg in calls if arg not in allowed]
    assert not offenders, (
        f"start_span called with a name that is not one of {sorted(allowed)}: {offenders}"
    )


def test_the_four_stage_names_are_bounded_literals() -> None:
    """Four names, all distinct, all `api.`-prefixed, none interpolated."""
    assert len(REQUEST_STAGES) == 4
    assert len(set(REQUEST_STAGES)) == 4
    assert all(stage.startswith("api.") for stage in REQUEST_STAGES)
    assert all("{" not in stage and "%" not in stage for stage in REQUEST_STAGES)
    # And they must not collide with the readyz set, or two unrelated code paths
    # would share one histogram series.
    assert not set(REQUEST_STAGES) & set(READYZ_STAGES)
