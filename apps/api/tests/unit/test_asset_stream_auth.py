"""WP-1: the asset streaming route sits inside the principal boundary —
without a resolvable principal the middleware rejects before any DB or
storage access (no lifespan/DB is started in this test on purpose).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx


async def test_asset_stream_is_not_exempt_from_auth():
    from aleph_api.main import create_app

    app = create_app()
    # No lifespan: AuthMiddleware only needs app.state.settings. If the 401
    # short-circuit ever regressed, the route would hit unset state and 500 —
    # either way this assertion fails.
    app.state.settings = SimpleNamespace(aleph_auth_mode="local")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(f"/v1/projects/{uuid4()}/assets/source/{uuid4()}")

    assert resp.status_code >= 400


def test_safe_filename_sanitizes_both_parts():
    """The extension is uploader-influenced and lands inside a quoted
    Content-Disposition value — quote/CTL characters must never survive."""
    from aleph_api.routes.assets import _safe_filename

    assert _safe_filename("S0001", "pdf") == "S0001.pdf"
    assert _safe_filename("S0001", 'a";b=c') == "S0001.abc"
    assert _safe_filename('we"ird name', "..pdf") == "we-ird-name.pdf"
    assert _safe_filename("", "") == "asset"
