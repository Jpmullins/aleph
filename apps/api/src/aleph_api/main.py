"""FastAPI app factory.

Compose-time entrypoint:
    uvicorn aleph_api.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

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
    aiq_internal,
    aliases,
    artifacts,
    assets,
    assistant,
    briefs,
    cards,
    changes,
    chunks,
    connector_credentials,
    connectors,
    cost,
    datasets,
    evals,
    feedback,
    handedits,
    health,
    hypotheses,
    ledger,
    me,
    merge_proposals,
    model_profile,
    notes,
    projects,
    reviews,
    scholar,
    smoketest,
    sources,
    surfaces,
    synthesize,
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
    app.include_router(agent_events.router)
    app.include_router(ledger.router)
    app.include_router(cost.router)
    app.include_router(model_profile.router)
    app.include_router(agent_tokens.router)
    app.include_router(smoketest.router)
    # Inc 1
    app.include_router(sources.router)
    app.include_router(assets.router)
    app.include_router(chunks.router)
    app.include_router(wiki.router)
    app.include_router(handedits.router)
    app.include_router(feedback.router)
    app.include_router(aliases.router)
    app.include_router(connectors.router)
    # Inc 2
    app.include_router(assistant.router)
    # Inc 3
    app.include_router(synthesize.router)
    app.include_router(merge_proposals.router)
    app.include_router(connector_credentials.router)
    app.include_router(aiq_internal.router)
    # Inc 4
    app.include_router(notes.router)
    app.include_router(cards.router)
    app.include_router(agent_actions.router)
    app.include_router(briefs.router)
    app.include_router(surfaces.router)
    app.include_router(changes.router)
    # Inc 5
    app.include_router(reviews.router)
    app.include_router(hypotheses.router)
    # Inc 6
    app.include_router(datasets.router)
    # Inc 7
    app.include_router(artifacts.router)
    # Inc 8
    app.include_router(evals.router)
    # WP-2 — verified scholarship (DOI verification, scholarly search,
    # citation expansion, Consensus evidence search)
    app.include_router(scholar.router)

    # Wave 2 — the assistant Deep Agent is mounted as an AG-UI endpoint at
    # /copilotkit/agent/assistant during lifespan startup (NOT here): its
    # Postgres-backed memory store (Wave 6 D1) must be built inside the running
    # event loop. The Node aleph-copilot-runtime bridges the React app to it.

    instrument_fastapi(app)

    return app
