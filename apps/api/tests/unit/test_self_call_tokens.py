"""WP-5 A4 — agent self-calls use real short-lived agent tokens.

Two guarantees:
  1. No hardcoded local-dev bearer sentinel remains in server production code
     (`apps/api/src`, `apps/workers/src`, `packages`). The only tolerated
     occurrence is the frontend local-mode sentinel in `apps/web`.
  2. The self-call header minter (`copilot_agent._self_headers`) produces a real
     HS256 agent token that `verify_agent_token` accepts and that is NOT the
     sentinel — so self-calls authenticate in oidc mode, not just local mode.
"""

from __future__ import annotations

import pathlib
from uuid import uuid4

from aleph_security.agent_token import verify_agent_token

_REPO = pathlib.Path(__file__).resolve().parents[4]
_SENTINEL = "Bearer local-" + "dev"  # split so this assertion file isn't self-flagged


def test_no_local_dev_sentinel_in_server_code() -> None:
    roots = [
        _REPO / "apps/api/src",
        _REPO / "apps/workers/src",
        _REPO / "packages",
    ]
    offenders: list[str] = []
    for root in roots:
        for f in root.rglob("*.py"):
            if "/tests/" in str(f) or f.name.startswith("test_"):
                continue
            text = f.read_text(errors="ignore")
            if _SENTINEL in text:
                offenders.append(str(f.relative_to(_REPO)))
    assert not offenders, f"local-dev bearer sentinel still in server code: {offenders}"


async def test_self_headers_mints_verifiable_non_sentinel_token() -> None:
    from aleph_api import copilot_agent

    # No session_maker bound → user id falls back to the stable dev uuid, so no
    # DB is needed to exercise the mint→verify round trip.
    copilot_agent.get_runtime()["session_maker"] = None

    class _Settings:
        aleph_agent_token_secret = "unit-test-secret-that-is-long-enough-32b"
        local_dev_subject = "local-dev"

    project_id = uuid4()
    headers = await copilot_agent._self_headers(project_id, settings=_Settings())  # pyright: ignore[reportPrivateUsage]

    auth = headers["Authorization"]
    assert auth.startswith("Bearer ")
    token = auth.split(" ", 1)[1]
    assert token != "local-dev", "self-call must not use the sentinel bearer"

    claims = verify_agent_token(token, secret="unit-test-secret-that-is-long-enough-32b")
    assert claims.project_id == project_id
    assert claims.actor_kind == "aleph_agent"
    assert claims.exp > claims.iat
