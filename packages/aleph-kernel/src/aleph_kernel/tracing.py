"""The kernel's tracing seam. stdlib only, and inert until something fills it.

The kernel wants two spans — `kernel.activate` and `kernel.effect.unwind` — and
that want made it import `aleph-observability`, which pulls eight OpenTelemetry
distributions and an LLM-observability vendor SDK. **A loader that cannot be
imported without a tracing vendor is not a loader**, and the constraint is that
the core depends on nothing above it.

So the kernel declares the SHAPE it needs and someone else supplies it.
`aleph-runtime` calls :func:`set_span_factory` at composition time with
`aleph_observability.tracing.start_span`; until then every span is a no-op that
still accepts attributes. Nothing branches on whether tracing is installed.

**The default is a real object, not `None`.** A `None` factory would put an
`if _factory is not None` at both call sites, and the untraced path would then
be the one nobody exercises — the shape that produces "it works in production
and crashes in a test with no exporter". `_NullSpan` makes the two paths
identical.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol


class Span(Protocol):
    """The part of a span the kernel uses. Structural, so any tracer fits."""

    def set_attribute(self, key: str, value: Any) -> Any: ...


class SpanFactory(Protocol):
    def __call__(self, name: str, **attrs: Any) -> Any: ...


class _NullSpan:
    """Accepts attributes and records nothing."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None


@contextmanager
def _null_factory(name: str, **attrs: Any) -> Iterator[_NullSpan]:
    del name, attrs
    yield _NullSpan()


_factory: SpanFactory = _null_factory


def set_span_factory(factory: SpanFactory | None) -> None:
    """Install the tracer. `None` restores the no-op, which is what a test wants."""
    global _factory
    _factory = factory or _null_factory


def start_span(name: str, **attrs: Any) -> Any:
    """Open a span through whatever factory is installed."""
    return _factory(name, **attrs)
