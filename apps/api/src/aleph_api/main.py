"""FastAPI app factory.

Compose-time entrypoint:
    uvicorn aleph_api.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aleph_observability.tracing import instrument_fastapi

from aleph_api.lifespan import lifespan
from aleph_api.middleware.auth import AuthMiddleware
from aleph_api.middleware.errors import ErrorMiddleware
from aleph_api.middleware.request_id import RequestIDMiddleware
from aleph_api.routes import (
    agent_tokens,
    aliases,
    assistant,
    chunks,
    connectors,
    cost,
    feedback,
    handedits,
    health,
    ledger,
    me,
    model_profile,
    projects,
    smoketest,
    sources,
    wiki,
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="aleph-api",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — local dev is permissive; production env should override via the
    # ingress, not here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    # Order matters: ErrorMiddleware is outermost so it catches everything;
    # AuthMiddleware runs after RequestIDMiddleware so we have a request_id
    # bound to logs before auth resolves.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(ErrorMiddleware)

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(projects.router)
    app.include_router(ledger.router)
    app.include_router(cost.router)
    app.include_router(model_profile.router)
    app.include_router(agent_tokens.router)
    app.include_router(smoketest.router)
    # Inc 1
    app.include_router(sources.router)
    app.include_router(chunks.router)
    app.include_router(wiki.router)
    app.include_router(handedits.router)
    app.include_router(feedback.router)
    app.include_router(aliases.router)
    app.include_router(connectors.router)
    # Inc 2
    app.include_router(assistant.router)

    instrument_fastapi(app)

    return app
