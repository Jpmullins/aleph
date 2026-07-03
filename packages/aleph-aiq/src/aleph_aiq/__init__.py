"""Aleph integration with NVIDIA AIQ (research subsystem).

Aleph runs AIQ as a separate worker process (`aiq-server`) and talks to
it over HTTP. AIQ never writes to Aleph's Postgres or asset storage directly —
all tool calls re-enter `aleph-api` via the `/internal/v1/aiq/*`
callbacks (auth bridge below).
"""

from aleph_aiq.auth_bridge import (
    AIQServiceToken,
    issue_service_token,
    verify_service_token,
)
from aleph_aiq.client import AIQClient, AIQJobStatus
from aleph_aiq.config_generator import emit_config
from aleph_aiq.tokenomics_adapter import (
    PhaseStat,
    record_aiq_phase_stats,
)

__all__ = [
    "AIQClient",
    "AIQJobStatus",
    "AIQServiceToken",
    "PhaseStat",
    "emit_config",
    "issue_service_token",
    "record_aiq_phase_stats",
    "verify_service_token",
]
