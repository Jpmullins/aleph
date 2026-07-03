"""Health and readiness endpoints. Public (no auth)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    checks: dict[str, dict[str, Any]] = {}

    # ---- Postgres ----
    try:
        maker = request.app.state.session_maker
        async with maker() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = {"ok": True}
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": str(exc)}

    # ---- Redis ----
    try:
        await request.app.state.redis.ping()
        checks["redis"] = {"ok": True}
    except Exception as exc:
        checks["redis"] = {"ok": False, "error": str(exc)}

    # ---- Asset store (writes a probe blob through the configured backend) ----
    try:
        store = request.app.state.asset_store
        probe = store.put_bytes(key=".readyz/probe", data=b"ok", mime_type="text/plain")
        checks["asset_store"] = {"ok": store.get(probe.storage_uri) == b"ok"}
    except Exception as exc:
        checks["asset_store"] = {"ok": False, "error": str(exc)}

    # ---- LiteLLM gateway ----
    try:
        ok = await request.app.state.litellm.health()
        checks["litellm_gateway"] = {"ok": ok}
    except Exception as exc:
        checks["litellm_gateway"] = {"ok": False, "error": str(exc)}

    all_ok = all(c.get("ok") for c in checks.values())
    code = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    body = {"status": "ready" if all_ok else "not_ready", "checks": checks}
    return JSONResponse(body, status_code=code)
