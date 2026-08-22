"""The meter has to produce series, and they have to MOVE.

A metric that does not change when the thing it measures changes is decoration,
and decoration on a dashboard is worse than a blank panel: somebody trusts it.
Every test here reads a counter, does something, and reads it again — never an
absolute value, because counters are cumulative and this process is shared with
every other test in the session.

The other half of this file is cardinality. `docs/plan.md` WS-P9 names it as the
risk: "any label carrying a project id, a user id or a raw path with a UUID in
it grows without bound". So there is a test that a raw path can never become a
label, and a test that a caller passing unbounded values gets collapsed rather
than obeyed.
"""

from __future__ import annotations

from decimal import Decimal

from aleph_observability import metrics as m


def _count(name: str, **labels: str) -> float:
    """A series' value, treating "no such series yet" as zero."""
    value = m.sample_value(name, **labels)
    return 0.0 if value is None else value


# ---------------------------------------------------------------------------
# The endpoint exposes real series
# ---------------------------------------------------------------------------


def test_exposition_is_prometheus_text_and_names_aleph_series() -> None:
    """Criterion 1's shape: `grep -c '^aleph_'` has to find real lines."""
    m.record_http_request(route="/v1/projects", method="GET", status=200, duration_s=0.01)
    m.record_llm_request(capability="chat", purpose="test.exposition", outcome="ok", duration_s=0.5)
    m.record_llm_usage(
        capability="chat",
        purpose="test.exposition",
        pricing_source="gateway",
        input_tokens=10,
        output_tokens=3,
        cost_usd=Decimal("0.0004"),
    )
    m.record_stage(stage="test.stage", outcome="ok", duration_s=0.02)
    m.replace_queue_depths({"default": 3})
    m.replace_agent_run_counts({("chunk_embed", "running"): 2})

    payload, content_type = m.render_prometheus()
    text = payload.decode()

    assert "text/plain" in content_type, content_type
    aleph_lines = [line for line in text.splitlines() if line.startswith("aleph_")]
    assert len(aleph_lines) >= 12, (
        f"only {len(aleph_lines)} aleph_ sample lines in the exposition; "
        "WS-P9 criterion 1 asks for at least 12"
    )
    for family in (
        m.HTTP_REQUESTS,
        m.HTTP_DURATION,
        m.LLM_REQUESTS,
        m.LLM_TOKENS,
        m.LLM_COST,
        m.QUEUE_DEPTH,
        m.AGENT_RUNS,
        m.STAGE_DURATION,
    ):
        assert f"# TYPE {family}" in text, f"{family} is not in the exposition"


# ---------------------------------------------------------------------------
# The counters move
# ---------------------------------------------------------------------------


def test_llm_failures_and_successes_are_separate_series() -> None:
    """The point of the `outcome` label.

    A counter that only increments on success cannot tell "the gateway is down"
    from "nobody is calling it", and those two need opposite responses. This is
    the assertion the review step's break-the-gateway run reproduces against the
    live stack.
    """
    labels = {"capability": "chat", "purpose": "test.outcome"}
    ok_before = _count(m.LLM_REQUESTS, **labels, outcome="ok")
    err_before = _count(m.LLM_REQUESTS, **labels, outcome="error")

    m.record_llm_request(**labels, outcome="error", duration_s=0.1)
    m.record_llm_request(**labels, outcome="error", duration_s=0.1)

    assert _count(m.LLM_REQUESTS, **labels, outcome="error") == err_before + 2
    assert _count(m.LLM_REQUESTS, **labels, outcome="ok") == ok_before, (
        "a failed call moved the success counter — the two outcomes are not "
        "separable and the metric cannot report an outage"
    )


def test_cost_is_attributed_to_how_it_was_priced() -> None:
    """Backlog E4 — "what share of spend is unpriced" — becomes one query."""
    for source, amount in (("gateway", "0.10"), ("unknown", "0.00")):
        m.record_llm_usage(
            capability="chat",
            purpose="test.pricing",
            pricing_source=source,
            input_tokens=100,
            output_tokens=50,
            cost_usd=Decimal(amount),
        )

    text = m.render_prometheus()[0].decode()
    for source in ("gateway", "unknown"):
        assert f'pricing_source="{source}"' in text, (
            f"no {m.LLM_COST} series labelled pricing_source={source}; the "
            "unpriced share is unanswerable"
        )

    tokens = _count(
        m.LLM_TOKENS,
        capability="chat",
        purpose="test.pricing",
        pricing_source="unknown",
        kind="input",
    )
    assert tokens >= 100, "unpriced tokens are not being counted"


def test_stage_latency_is_recorded_per_stage() -> None:
    before = _count(f"{m.STAGE_DURATION}_count", stage="test.ingest", outcome="ok")
    m.record_stage(stage="test.ingest", outcome="ok", duration_s=0.3)
    assert _count(f"{m.STAGE_DURATION}_count", stage="test.ingest", outcome="ok") == before + 1


# ---------------------------------------------------------------------------
# Pull-model gauges
# ---------------------------------------------------------------------------


def test_a_drained_queue_stops_being_reported_rather_than_freezing() -> None:
    """`replace_*` swaps the whole family; an update-in-place would not.

    A gauge stuck at its last non-zero reading is worse than no gauge — it
    reports a backlog that has already cleared, which is exactly the direction
    that produces a wrong decision.
    """
    m.replace_queue_depths({"default": 7, "code_runner": 2})
    assert _count(m.QUEUE_DEPTH, queue="default") == 7

    m.replace_queue_depths({"default": 0})
    assert _count(m.QUEUE_DEPTH, queue="default") == 0
    assert m.sample_value(m.QUEUE_DEPTH, queue="code_runner") is None, (
        "a queue that disappeared from the snapshot is still being reported"
    )


def test_agent_run_counts_are_keyed_by_kind_and_status() -> None:
    m.replace_agent_run_counts({("chunk_embed", "succeeded"): 40, ("chunk_embed", "failed"): 45})
    assert _count(m.AGENT_RUNS, agent_kind="chunk_embed", status="failed") == 45
    assert _count(m.AGENT_RUNS, agent_kind="chunk_embed", status="succeeded") == 40


# ---------------------------------------------------------------------------
# Cardinality
# ---------------------------------------------------------------------------


def test_a_caller_supplied_label_is_capped_rather_than_obeyed() -> None:
    """One call site passing an id must not be able to grow series forever."""
    # The guard itself is the unit under test, hence the private name.
    guard = m._LabelGuard("stage", ceiling=3)
    assert guard("a") == "a"
    assert guard("b") == "b"
    assert guard("c") == "c"
    assert guard("a") == "a", "a value already seen must keep its own label"
    assert guard("d") == m.OVERFLOW_LABEL, (
        "the fourth distinct value was accepted; a metrics endpoint with an "
        "unbounded label is an outage waiting for enough traffic"
    )
    assert guard.distinct() == 3


#: A path with an id in it. If this string ever reaches a label, the endpoint
#: grows one series per project and stops being scrapeable.
_PATH_WITH_AN_ID = "/v1/projects/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/sources"


def test_route_template_never_falls_back_to_the_raw_path_when_nothing_matched() -> None:
    """The 404 shape: no route matched, so the scope carries no endpoint.

    This is where a fallback is most tempting — "we know the path, use it" —
    and it is the shape a vulnerability scanner produces a thousand times a
    minute.
    """
    scope = {"type": "http", "path": _PATH_WITH_AN_ID}
    assert m.route_template(scope) == m.UNMATCHED_ROUTE


def test_route_template_never_falls_back_when_the_endpoint_is_unrecognised() -> None:
    """The other branch, and the one a lookup-miss fallback would live in.

    An app whose route table does not contain this endpoint — a route
    registered after the table was cached, a mounted sub-app — still must not
    produce a per-id label. Asserted separately because the no-endpoint case
    returns early and cannot reach the lookup at all.
    """

    class _App:
        def __init__(self) -> None:
            self.routes: list[object] = []

    def _endpoint() -> None: ...

    scope = {
        "type": "http",
        "path": _PATH_WITH_AN_ID,
        "app": _App(),
        "endpoint": _endpoint,
    }
    assert m.route_template(scope) == m.UNMATCHED_ROUTE, (
        "an endpoint missing from the route table fell back to the raw path; "
        "that is one metric series per project id"
    )


# ---------------------------------------------------------------------------
# `start_span` is the ingest/retrieval latency source
# ---------------------------------------------------------------------------


def test_start_span_records_the_stage_histogram() -> None:
    """Ingest and retrieval get a latency number without a second pass.

    `worker.normalize`, `worker.chunk_embed` and `assistant.retrieve` are
    already `start_span` call sites in packages this workstream does not own.
    Emitting the histogram from `start_span` itself is what turns those into
    measured stages; if this stops happening, ingest and retrieval latency go
    silently unmeasured while every span still looks fine in a trace viewer.
    """
    from aleph_observability.tracing import start_span

    before = _count(f"{m.STAGE_DURATION}_count", stage="test.span.ok", outcome="ok")
    with start_span("test.span.ok"):
        pass
    assert _count(f"{m.STAGE_DURATION}_count", stage="test.span.ok", outcome="ok") == before + 1


def test_a_failing_stage_is_recorded_as_an_error_not_dropped() -> None:
    """A stage that raises is the one worth timing, and the naive
    implementation — record after the body — records nothing at all for it."""
    from aleph_observability.tracing import start_span

    before = _count(f"{m.STAGE_DURATION}_count", stage="test.span.boom", outcome="error")
    try:
        with start_span("test.span.boom"):
            msg = "deliberate"
            raise RuntimeError(msg)
    except RuntimeError:
        pass
    assert (
        _count(f"{m.STAGE_DURATION}_count", stage="test.span.boom", outcome="error") == before + 1
    )
