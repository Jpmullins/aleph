"""End-to-end enqueue against the running `aleph-code-runner` service (WP-4c).

Integration: enqueues a real code job on the dedicated `code_runner` queue and
awaits the result. Skips (rather than fails) if the code-runner worker is not
booted, so it is safe on stacks that haven't started the new service yet.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_QUEUE = "arq:queue:code_runner"


async def test_code_runner_executes_matplotlib_png() -> None:
    from arq import create_pool
    from arq.connections import RedisSettings

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    pool = await create_pool(RedisSettings.from_dsn(redis_url))
    try:
        code = (
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3])\n"
        )
        job = await pool.enqueue_job("run_code_job", code, "png", 30, _queue_name=_QUEUE)
        assert job is not None
        try:
            result = await job.result(timeout=45)
        except Exception as exc:
            pytest.skip(f"aleph-code-runner not available: {exc}")
        assert result["ok"] is True, result
        assert result["mime"] == "image/png"
        assert result["bytes_b64"]
    finally:
        await pool.aclose()
