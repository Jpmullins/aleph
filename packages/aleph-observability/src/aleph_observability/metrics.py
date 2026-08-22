"""An OTEL meter, and a Prometheus exposition of it.

Tracing answers *what happened in this one request*. It cannot answer *is this
getting slower*, *how often does this fail*, or *what share of spend is
unpriced* — those are questions about a population, and until this module
existed Aleph could not answer any of them. `docs/plan.md` WS-P9 names the cost
directly: "gateway rate limiting, reported as weirdly rate limited, not yet
characterised" is unanswerable by reading logs and trivial with a per-purpose
request counter.

Five things are instrumented, and deliberately only five:

  1. HTTP request rate, latency and status **by route template**.
  2. LLM calls by capability, purpose and outcome.
  3. Tokens and cost by ``pricing_source`` — so "how much of our spend is
     unpriced" is one query rather than an anecdote.
  4. Queue depth (arq) and background-run outcome.
  5. Stage latency for every `start_span` call site — which is how ingest,
     the wiki curator, the reviewers and the builder get a latency number
     without a second instrumentation pass. See `tracing.start_span`.

## Label cardinality is the failure mode

A metrics endpoint becomes an outage the moment a label carries an unbounded
value: a project id, a user id, or a raw request path with a UUID in it. Every
series is retained for the process lifetime, so one bad label grows memory
without bound and makes the exposition unparseable long before that.

So: **the route label is the template** (``/v1/projects/{project_id}/sources``),
never `scope["path"]`, and there is no code path here that falls back to the
raw path — an unrecognised request is labelled ``<unmatched>`` and stays one
series. `capability`, `purpose`, `pricing_source`, `queue`, `agent_kind`,
`status` and `stage` are all drawn from literals in the source. If you add a
label, the question to answer first is "what is the largest number of distinct
values this can take", and the answer has to be a number.

## Why the provider is built lazily

`init_otel` installs the tracer provider with the service `Resource` (name,
version, environment, profile) during lifespan startup. The meter wants the
same resource, and this module is imported long before that runs. So the meter
provider is created on first *use* — the first recorded measurement or the
first scrape — by which time the tracer provider exists and its resource can be
adopted. `init_metrics()` is still available for a caller that wants to force
it earlier (a kernel capability with a probe, for instance).

## Why a private registry

`PrometheusMetricReader` defaults to `prometheus_client`'s global `REGISTRY`,
which also carries the interpreter's GC and platform collectors. A private
registry keeps the exposition to what Aleph actually measures and makes a test
that builds a second provider deterministic instead of colliding with whatever
ran before it.

## What this does NOT cover

Metrics are per-process, because a Prometheus scrape is per-process. This
module is mounted in the API (`/metrics`). The arq worker records into its own
in-process meter with nothing exposing it — `aleph_llm_*` from a worker job is
counted and never scraped. Giving the worker its own exposition is a separate
change; do not read the API's numbers as covering the whole system.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakKeyDictionary

import structlog
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import CallbackOptions, Counter, Histogram, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_log = structlog.get_logger(__name__)

_METER_NAME = "aleph"

# ---------------------------------------------------------------------------
# Metric names. Exported so tests and the acceptance gate name one string.
# ---------------------------------------------------------------------------

HTTP_REQUESTS: Final = "aleph_http_requests_total"
HTTP_DURATION: Final = "aleph_http_request_duration_seconds"
LLM_REQUESTS: Final = "aleph_llm_requests_total"
LLM_DURATION: Final = "aleph_llm_request_duration_seconds"
LLM_TOKENS: Final = "aleph_llm_tokens_total"
LLM_COST: Final = "aleph_model_call_cost_total"
QUEUE_DEPTH: Final = "aleph_queue_depth"
AGENT_RUNS: Final = "aleph_agent_runs"
STAGE_DURATION: Final = "aleph_stage_duration_seconds"

#: The label a request gets when no route matched it — a 404, or a probe for
#: `/wp-login.php`. It exists so there is exactly one series for "everything we
#: do not serve" instead of one per URL an attacker feels like trying.
UNMATCHED_ROUTE: Final = "<unmatched>"

#: What a caller-supplied label collapses to once its guard is full.
OVERFLOW_LABEL: Final = "<overflow>"

#: Distinct values one guarded label may take before it collapses. Two hundred
#: is far above every real value set (there are ~45 span names and ~30 call
#: purposes in the tree) and far below the point where the exposition stops
#: being parseable.
_LABEL_CEILING: Final = 200

#: Seconds. Wide on purpose: the same boundaries serve an 8ms wiki read and a
#: 90s deep-research LLM call, and two bucket sets would mean two answers to
#: "what is p95" depending on which instrument you asked.
_DURATION_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)


# ---------------------------------------------------------------------------
# Label guards
#
# `route` is bounded by the route table and `capability` / `pricing_source` /
# `outcome` by enums. `stage` and `purpose` are not: they are strings a call
# site passes, and the cheapest way to take this endpoint down is to pass one
# with an id in it. The guard makes that a bounded, loud failure — the label
# collapses to `<overflow>` and the log names the offender — instead of an
# unbounded one that nothing reports until the process runs out of memory.
# ---------------------------------------------------------------------------


class _LabelGuard:
    """Caps the distinct values of one caller-supplied label."""

    def __init__(self, label: str, ceiling: int = _LABEL_CEILING) -> None:
        self._label = label
        self._ceiling = ceiling
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._warned = False

    def __call__(self, value: str) -> str:
        with self._lock:
            if value in self._seen:
                return value
            if len(self._seen) < self._ceiling:
                self._seen.add(value)
                return value
            warned = self._warned
            self._warned = True
        if not warned:
            _log.error(
                "metrics.label_cardinality_exceeded",
                label=self._label,
                ceiling=self._ceiling,
                offending_value=value,
                remediation=(
                    "a metric label is carrying an unbounded value (an id, a path, "
                    "a query). Find the call site and pass a literal."
                ),
            )
        return OVERFLOW_LABEL

    def distinct(self) -> int:
        with self._lock:
            return len(self._seen)


_stage_label = _LabelGuard("stage")
_purpose_label = _LabelGuard("purpose")


# ---------------------------------------------------------------------------
# Pull-model gauge state
#
# An OTEL observable-gauge callback is synchronous, and both of the gauges here
# are read from a network service (Redis, Postgres). So the async scrape path
# refreshes these snapshots first and the sync callback serves what it finds.
# `replace_*` swaps the WHOLE family rather than updating keys, because a
# label set that disappears — a queue that drained, a status with no rows left
# — must stop being reported rather than freeze at its last value. A gauge
# stuck at its last non-zero reading is worse than no gauge: it reports a
# backlog that cleared.
# ---------------------------------------------------------------------------

_snapshot_lock = threading.Lock()
_queue_depths: dict[str, int] = {}
_agent_run_counts: dict[tuple[str, str], int] = {}


def replace_queue_depths(depths: Mapping[str, int]) -> None:
    """Publish the current arq queue depths. Replaces the whole family."""
    with _snapshot_lock:
        _queue_depths.clear()
        _queue_depths.update(depths)


def replace_agent_run_counts(counts: Mapping[tuple[str, str], int]) -> None:
    """Publish background-run counts keyed by ``(agent_kind, status)``."""
    with _snapshot_lock:
        _agent_run_counts.clear()
        _agent_run_counts.update(counts)


def _observe_queue_depth(_options: CallbackOptions) -> Iterable[Observation]:
    with _snapshot_lock:
        items = list(_queue_depths.items())
    return [Observation(value, {"queue": queue}) for queue, value in items]


def _observe_agent_runs(_options: CallbackOptions) -> Iterable[Observation]:
    with _snapshot_lock:
        items = list(_agent_run_counts.items())
    return [
        Observation(value, {"agent_kind": kind, "status": status})
        for (kind, status), value in items
    ]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class _Instruments:
    """Created once, with the provider, so every record hits the same meter."""

    def __init__(self, meter: otel_metrics.Meter) -> None:
        self.http_requests: Counter = meter.create_counter(
            HTTP_REQUESTS,
            description="HTTP requests served, by route template, method and status.",
        )
        self.http_duration: Histogram = meter.create_histogram(
            HTTP_DURATION,
            unit="s",
            description="Wall-clock time to serve an HTTP request.",
        )
        self.llm_requests: Counter = meter.create_counter(
            LLM_REQUESTS,
            description="Gateway calls, by capability, purpose and outcome.",
        )
        self.llm_duration: Histogram = meter.create_histogram(
            LLM_DURATION,
            unit="s",
            description="Wall-clock time of a gateway call.",
        )
        self.llm_tokens: Counter = meter.create_counter(
            LLM_TOKENS,
            description="Tokens billed, by kind and by how the call was priced.",
        )
        self.llm_cost: Counter = meter.create_counter(
            LLM_COST,
            description="USD spent, by how the rate was obtained (gateway/static/unknown).",
        )
        self.stage_duration: Histogram = meter.create_histogram(
            STAGE_DURATION,
            unit="s",
            description="Wall-clock time of a named pipeline stage (one per traced span).",
        )
        # Observable: read at scrape time, not written by the code being
        # measured. A queue depth counted at write time would be wrong the
        # moment a worker restarted.
        meter.create_observable_gauge(
            QUEUE_DEPTH,
            callbacks=[_observe_queue_depth],
            description="Jobs waiting in an arq queue, read from Redis at scrape time.",
        )
        meter.create_observable_gauge(
            AGENT_RUNS,
            callbacks=[_observe_agent_runs],
            description="Background runs by kind and status, read from Postgres at scrape time.",
        )


_provider_lock = threading.Lock()
_provider: MeterProvider | None = None
_registry: CollectorRegistry | None = None
_instruments: _Instruments | None = None


def _resource() -> Resource:
    """Adopt the tracer's resource so metrics and traces name the same service.

    Falls back to an empty resource rather than inventing a service name: a
    metric labelled `unknown_service` is at least honest about not knowing,
    where a guessed name silently merges two processes' series.
    """
    provider = trace.get_tracer_provider()
    resource = getattr(provider, "resource", None)
    if isinstance(resource, Resource):
        return resource
    return Resource.create({})


def init_metrics() -> _Instruments:
    """Build the meter provider, its Prometheus reader and the instruments.

    Idempotent, and safe to call from any thread. `set_meter_provider` refuses
    to be overridden once set, so this must run exactly once per process — the
    lock and the `_provider` guard are what make "call it lazily from wherever
    needs it first" a correct strategy rather than a race.
    """
    global _provider, _registry, _instruments
    with _provider_lock:
        if _instruments is not None:
            return _instruments
        registry = CollectorRegistry()
        reader = PrometheusMetricReader(registry=registry)
        views = [
            View(
                instrument_name=name,
                aggregation=ExplicitBucketHistogramAggregation(_DURATION_BUCKETS),
            )
            for name in (HTTP_DURATION, LLM_DURATION, STAGE_DURATION)
        ]
        provider = MeterProvider(resource=_resource(), metric_readers=[reader], views=views)
        otel_metrics.set_meter_provider(provider)
        _provider = provider
        _registry = registry
        _instruments = _Instruments(provider.get_meter(_METER_NAME))
        return _instruments


def shutdown_metrics() -> None:
    """Tear the provider down. Only meaningful at process exit."""
    global _provider, _registry, _instruments
    with _provider_lock:
        if _provider is not None:
            _provider.shutdown()
        _provider = None
        _registry = None
        _instruments = None


def _inst() -> _Instruments:
    return _instruments if _instruments is not None else init_metrics()


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------


def render_prometheus() -> tuple[bytes, str]:
    """Render the current state in Prometheus exposition format.

    Returns ``(payload, content_type)``. Rendering drives a collection, which
    is what fires the observable-gauge callbacks — so refresh the pull-model
    snapshots *before* calling this, not after.
    """
    init_metrics()
    registry = _registry
    if registry is None:  # pragma: no cover — init_metrics just set it
        msg = "metrics registry missing after init"
        raise RuntimeError(msg)
    return generate_latest(registry), CONTENT_TYPE_LATEST


def sample_value(name: str, **labels: str) -> float | None:
    """One series' current value, or None if that series does not exist.

    For tests and for a caller that wants to assert a counter moved. Counters
    are cumulative, so "it moved" is a difference between two reads, never an
    absolute value.
    """
    init_metrics()
    registry = _registry
    if registry is None:  # pragma: no cover
        return None
    return registry.get_sample_value(name, labels or None)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record_http_request(*, route: str, method: str, status: int, duration_s: float) -> None:
    """One served HTTP request. `route` MUST be a template — see module docs."""
    inst = _inst()
    inst.http_requests.add(1, {"route": route, "method": method, "status": str(status)})
    inst.http_duration.record(duration_s, {"route": route, "method": method})


def record_llm_request(
    *,
    capability: str,
    purpose: str,
    outcome: str,
    duration_s: float,
) -> None:
    """One gateway call. `outcome` is ``ok`` or ``error``.

    Called on the failure path too — that is the entire point. A counter that
    only increments on success cannot tell "the gateway is down" from "nobody
    is calling it", and those need opposite responses.
    """
    labels = {"capability": capability, "purpose": _purpose_label(purpose), "outcome": outcome}
    inst = _inst()
    inst.llm_requests.add(1, labels)
    inst.llm_duration.record(duration_s, labels)


def record_llm_usage(
    *,
    capability: str,
    purpose: str,
    pricing_source: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal | float,
) -> None:
    """Tokens and money for one priced call.

    `pricing_source` is the label the whole thing exists for: `gateway` (the
    rate was reported), `static` (asserted from an operator hints file) or
    `unknown` (unpriced, and therefore recorded as $0 in the ledger). The share
    of spend sitting in `unknown` is a number here instead of an anecdote.
    """
    inst = _inst()
    base = {
        "capability": capability,
        "purpose": _purpose_label(purpose),
        "pricing_source": pricing_source,
    }
    if input_tokens:
        inst.llm_tokens.add(input_tokens, {**base, "kind": "input"})
    if output_tokens:
        inst.llm_tokens.add(output_tokens, {**base, "kind": "output"})
    inst.llm_cost.add(float(cost_usd), base)


def record_stage(*, stage: str, outcome: str, duration_s: float) -> None:
    """Latency of a named pipeline stage. Fed by every `start_span` call site."""
    _inst().stage_duration.record(duration_s, {"stage": _stage_label(stage), "outcome": outcome})


# ---------------------------------------------------------------------------
# HTTP instrumentation
# ---------------------------------------------------------------------------

#: `endpoint -> route template`, per app. Weak-keyed so a test that builds a
#: hundred apps does not retain a hundred route tables.
_route_tables: WeakKeyDictionary[Any, tuple[int, dict[Any, str]]] = WeakKeyDictionary()


def _route_table(app: Any) -> dict[Any, str]:
    """Map every registered endpoint to its path template.

    Starlette puts `endpoint` in the ASGI scope when a route matches but not
    the template it came from, and re-matching the whole route list per request
    (what the upstream OTEL FastAPI instrumentation does) is a linear scan on
    every request. Building the map once is the same answer for less work.

    Keyed on the route count so a route registered after boot — the AG-UI agent
    endpoint is mounted during lifespan startup, after `create_app` returns —
    does not report `<unmatched>` forever.
    """
    routes: list[Any] = list(getattr(app, "routes", []))
    cached = _route_tables.get(app)
    if cached is not None and cached[0] == len(routes):
        return cached[1]
    table: dict[Any, str] = {}
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        path = getattr(route, "path", None)
        if endpoint is not None and isinstance(path, str):
            table.setdefault(endpoint, path)
        # A `Mount` carries its sub-app as the scope's endpoint; the mount path
        # is the right low-cardinality label for everything underneath it.
        sub_app = getattr(route, "app", None)
        if sub_app is not None and isinstance(path, str):
            table.setdefault(sub_app, path)
    _route_tables[app] = (len(routes), table)
    return table


def route_template(scope: Mapping[str, Any]) -> str:
    """The route template for a finished request, or `<unmatched>`.

    There is deliberately no fallback to `scope["path"]`. That fallback is how
    a metrics endpoint acquires one series per UUID and stops being scrapeable.
    """
    app = scope.get("app")
    endpoint = scope.get("endpoint")
    if app is None or endpoint is None:
        return UNMATCHED_ROUTE
    return _route_table(app).get(endpoint, UNMATCHED_ROUTE)
