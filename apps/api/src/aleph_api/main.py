"""FastAPI app factory.

Compose-time entrypoint:
    uvicorn aleph_api.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aleph_api.lifespan import lifespan
from aleph_api.middleware.auth import AuthMiddleware
from aleph_api.middleware.errors import ErrorMiddleware
from aleph_api.middleware.request_id import RequestIDMiddleware
from aleph_api.routes import (
    agent_actions,
    agent_events,
    agent_tokens,
    aliases,
    artifacts,
    assets,
    assistant,
    cards,
    changes,
    connector_credentials,
    connectors,
    cost,
    feedback,
    handedits,
    health,
    hypotheses,
    ledger,
    me,
    model_profile,
    notes,
    projects,
    reviews,
    scholar,
    smoketest,
    sources,
    surfaces,
    synthesize,
    viz,
    wiki,
)
from aleph_observability.tracing import instrument_fastapi


def create_app() -> FastAPI:
    app = FastAPI(
        title="aleph-api",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — local dev is permissive; production env should override via the
    # ingress, not here.
    #
    # Configurable because the origin is a property of where the browser loaded
    # the app from, not of the API. Serving the UI on a LAN or tailnet address
    # while this list said "localhost" blocked every request from the page it
    # was serving, with a CORS error that names the symptom and not the cause.
    # Comma-separated; `allow_credentials` forbids "*", so an explicit list is
    # the only correct answer here.
    origins = [
        o.strip()
        for o in os.environ.get("ALEPH_CORS_ORIGINS", "http://localhost:5173").split(",")
        if o.strip()
    ]
    # Order matters, and `add_middleware` PREPENDS, so the last one added is
    # the outermost. Reading bottom-up, the stack is:
    #     CORS -> RequestID -> Error -> Auth -> routes
    #
    # CORS must be outermost. It used to be added first, which put it INSIDE
    # ErrorMiddleware — so an unhandled exception produced a JSONResponse that
    # never passed back through CORS, and the browser saw a response with no
    # `Access-Control-Allow-Origin` header. Every server error then surfaced in
    # the console as a CORS failure instead of the actual error, which is the
    # worst possible way to report a 500: it names the wrong subsystem and
    # hides the traceback the developer needs.
    #
    # RequestID must be outside Error. It was inside, and an exception does not
    # come back as a response — it passes through that frame, skipping the line
    # that stamps `x-request-id`. So every 500 answered with no correlation id,
    # even when the caller had sent one, and a user's "I got a 500 at 14:22"
    # could not be joined to any line in the log. Outside, the request context
    # RequestID binds is still live while Error logs and formats, and the
    # problem response passes back out through the stamp on its way to CORS.
    #
    # Error must stay outside Auth, which is the middleware most likely to
    # raise (it talks to Postgres to resolve the principal). The one thing it
    # no longer wraps is RequestIDMiddleware itself — five lines that generate
    # a uuid and read a header — and that is the trade this ordering makes.
    #
    # Correlation does not RELY on this order: ErrorMiddleware reads the id,
    # principal and project out of the shared ASGI scope rather than inheriting
    # contextvars, because a BaseHTTPMiddleware's downstream bindings cannot
    # reach an upstream frame. The order is still pinned, by
    # `apps/api/tests/unit/test_request_correlation.py::test_middleware_order_is_pinned`
    # and `test_cors_survives_errors.py::test_cors_is_the_outermost_middleware`,
    # so a fourth middleware cannot land in the wrong slot in silence.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(ErrorMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(projects.router)
    app.include_router(agent_events.router)
    app.include_router(ledger.router)
    app.include_router(cost.router)
    app.include_router(model_profile.router)
    app.include_router(agent_tokens.router)
    app.include_router(smoketest.router)
    # Inc 1
    app.include_router(sources.router)
    app.include_router(assets.router)
    app.include_router(wiki.router)
    app.include_router(handedits.router)
    app.include_router(feedback.router)
    app.include_router(aliases.router)
    app.include_router(connectors.router)
    # Inc 2
    app.include_router(assistant.router)
    # Inc 3
    app.include_router(synthesize.router)
    app.include_router(connector_credentials.router)
    # Inc 4
    app.include_router(notes.router)
    app.include_router(cards.router)
    app.include_router(agent_actions.router)
    app.include_router(surfaces.router)
    app.include_router(changes.router)
    # Inc 5
    app.include_router(reviews.router)
    app.include_router(hypotheses.router)
    # Inc 7
    app.include_router(artifacts.router)
    app.include_router(viz.router)
    # WP-2 — verified scholarship (DOI verification, scholarly search,
    # citation expansion, Consensus evidence search)
    app.include_router(scholar.router)

    # Wave 2 — the assistant Deep Agent is mounted as an AG-UI endpoint at
    # /copilotkit/agent/assistant during lifespan startup (NOT here): its
    # Postgres-backed memory store (Wave 6 D1) must be built inside the running
    # event loop. The Node aleph-copilot-runtime bridges the React app to it.

    instrument_fastapi(app)

    return app
