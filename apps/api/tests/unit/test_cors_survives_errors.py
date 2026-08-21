"""Error responses must carry CORS headers, or every failure lies about itself.

`add_middleware` PREPENDS, so the LAST middleware added is the outermost. CORS
used to be added first, which made it the innermost of the four — inside
`ErrorMiddleware` and `AuthMiddleware`. A response produced by either of those
therefore never passed back out through CORS, so the browser received it with no
`Access-Control-Allow-Origin` and reported a CORS failure.

That is the worst way to surface a server error. It names the wrong subsystem,
sends whoever is debugging to look at origins and preflights, and hides the
status and body entirely. Saving a model binding 500'd for an unrelated reason
and reached the console as a CORS problem, which is where the real time went.

These use `oidc` mode with no credential: the 401 comes from `AuthMiddleware`
itself, which is precisely the class of response that lost its headers, and it
needs no database.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

ORIGIN = "http://localhost:5273"


@pytest.fixture(autouse=True)
def _allowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_app` reads the allow-list from the environment at build time.

    Without this the app allows only the default `http://localhost:5173`, so a
    request from ORIGIN is refused by CORS *correctly* and the assertions below
    would fail for a reason that has nothing to do with middleware ordering —
    which is exactly what happened the first time these were written.
    """
    monkeypatch.setenv("ALEPH_CORS_ORIGINS", ORIGIN)


def _app():
    from aleph_api.main import create_app

    app = create_app()
    app.state.settings = SimpleNamespace(aleph_auth_mode="oidc")
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/v1/projects/00000000-0000-0000-0000-000000000000/model-profile",
        "/v1/does-not-exist",
    ],
)
async def test_middleware_generated_errors_carry_cors(path: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    ) as client:
        resp = await client.get(path, headers={"Origin": ORIGIN})

    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == ORIGIN, (
        f"{path} returned {resp.status_code} with no CORS header. The browser "
        "reports that as a CORS failure and the real status is never seen."
    )


async def test_preflight_still_works() -> None:
    """Guard the guard: moving CORS must not break the preflight it exists for."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    ) as client:
        resp = await client.options(
            "/v1/projects/00000000-0000-0000-0000-000000000000/model-profile",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ORIGIN
    assert "PATCH" in (resp.headers.get("access-control-allow-methods") or "")


def test_cors_is_the_outermost_middleware() -> None:
    """The mechanism, not one symptom.

    Ordering here is positional and trivially undone by adding a middleware in
    the wrong place, and nothing about the code reads as wrong when it happens.
    """
    from fastapi.middleware.cors import CORSMiddleware

    app = _app()
    outermost = app.user_middleware[0]
    assert outermost.cls is CORSMiddleware, (
        f"outermost middleware is {outermost.cls.__name__}, not CORSMiddleware. "
        "`add_middleware` prepends, so CORS must be added LAST — otherwise "
        "responses generated above it reach the browser with no CORS headers."
    )
