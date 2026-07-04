"""aleph-code-runner — the isolated sandbox worker (WP-4 sub-spec c).

This package runs in its own compose service (`aleph-code-runner`) with no
credentials, no database/asset access, and no route to the internet. It pulls
code jobs off a dedicated Redis queue and executes agent-written Python in a
locked-down subprocess, returning only result bytes + metadata. It imports NO
aleph package on purpose — a full escape yields nothing but CPU/mem inside the
cgroup caps and the agent's own submitted code.
"""

from __future__ import annotations

from aleph_code_runner.executor import CODE_RUNNER_QUEUE, RunResult, run_agent_code

__all__ = ["CODE_RUNNER_QUEUE", "RunResult", "run_agent_code"]
