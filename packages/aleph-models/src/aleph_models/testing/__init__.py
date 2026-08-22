"""Test doubles for the model path, shared across packages.

Lives inside the installed distribution rather than under a `tests/` directory
because pytest conftest files are scoped to their own subtree — one under
`tests/` is never applied to `packages/aleph-models/tests/` — and a `sys.path`
shim is the pattern that was removed from `scripts/check-graph-state-keys.sh`
for making a gate depend on the test suite existing. A module on the normal
import path is the only placement that lets `apps/`, `packages/` and the
integration suite import the *same* fake instead of three that drift.

Nothing here is imported by shipped code. The Starlette dependency the fake
gateway needs is declared in the `testing` extra, so a production install of
`aleph-models` does not pull a web framework.
"""

from aleph_models.testing.cost import RecordingSessions
from aleph_models.testing.gateway import (
    DEFAULT_MODELS,
    FakeGateway,
    FakeModel,
    GatewayConfig,
    RecordedRequest,
    ScriptedResponse,
    rate_limited,
    server_error,
)

__all__ = [
    "DEFAULT_MODELS",
    "FakeGateway",
    "FakeModel",
    "GatewayConfig",
    "RecordedRequest",
    "RecordingSessions",
    "ScriptedResponse",
    "rate_limited",
    "server_error",
]
