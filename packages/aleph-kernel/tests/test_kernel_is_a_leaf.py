"""The kernel's independence, and the one duplication it costs.

`uuid7` is duplicated in `aleph_kernel.ids` rather than shared, deliberately:
anything that HOLDS a shared implementation becomes a package the kernel needs,
which recreates the dependency under a new name. Fifteen lines of bit-shuffling
is the cheaper trade — but only while the two agree, so that is what this pins.

A divergence would be quiet and expensive: ids that sort differently, or a
version nibble in the wrong place, showing up as a registry listing in the wrong
order rather than as an error.
"""

from __future__ import annotations

from uuid import UUID

from aleph_kernel.ids import uuid7 as kernel_uuid7


def test_the_kernel_mints_a_real_uuid7() -> None:
    value = kernel_uuid7()
    assert isinstance(value, UUID)
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"


def test_kernel_uuid7_matches_aleph_core() -> None:
    """Same layout, same version, same variant — checked structurally, because
    the values differ by construction (time and randomness)."""
    from aleph_core.ids import uuid7 as core_uuid7

    for _ in range(20):
        a, b = kernel_uuid7(), core_uuid7()
        assert a.version == b.version == 7
        assert a.variant == b.variant
        # Bytes 0-5 are the millisecond timestamp; two ids minted together must
        # agree to the second, which is what makes them sort together.
        assert a.bytes[:5] == b.bytes[:5]


def test_ids_are_time_ordered() -> None:
    """The property the format is chosen FOR: a registry listing is stable and a
    log is correlatable without a separate column."""
    import time

    first = kernel_uuid7()
    time.sleep(0.005)
    second = kernel_uuid7()
    assert first.bytes[:6] <= second.bytes[:6]


def test_the_tracing_seam_is_inert_until_filled() -> None:
    """The default must be a real object, not `None`.

    A `None` factory would put an `if installed` at both call sites, and the
    untraced path would be the one nobody exercises — which is how "works in
    production, crashes in a test with no exporter" happens.
    """
    from aleph_kernel.tracing import set_span_factory, start_span

    set_span_factory(None)
    with start_span("kernel.test", **{"aleph.capability": "x"}) as span:
        span.set_attribute("aleph.kernel.inverses", 3)  # must not raise


def test_an_installed_factory_receives_the_spans() -> None:
    from aleph_kernel.tracing import set_span_factory, start_span

    seen: list[tuple[str, dict[str, object]]] = []

    class _Span:
        def set_attribute(self, key: str, value: object) -> None:
            seen.append((key, {"value": value}))

    import contextlib

    @contextlib.contextmanager
    def factory(name: str, **attrs: object):
        seen.append((name, dict(attrs)))
        yield _Span()

    try:
        set_span_factory(factory)
        with start_span("kernel.activate", **{"aleph.capability": "db"}) as span:
            span.set_attribute("k", 1)
        assert seen[0] == ("kernel.activate", {"aleph.capability": "db"})
        assert seen[1][0] == "k"
    finally:
        set_span_factory(None)
