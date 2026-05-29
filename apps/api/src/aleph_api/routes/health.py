"""Health and readiness endpoints. Public (no auth)."""

from __future__ import annotations

from typing import Any

import httpx
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

    # ---- MinIO ----
    s = request.app.state.settings
    if s.minio_endpoint:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{s.minio_endpoint}/minio/health/live")
                checks["minio"] = {"ok": resp.status_code == 200}
        except Exception as exc:
            checks["minio"] = {"ok": False, "error": str(exc)}

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
