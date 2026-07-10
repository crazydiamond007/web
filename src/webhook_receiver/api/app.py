"""FastAPI application factory.

Note what is absent: any call to `Base.metadata.create_all`. The schema is
Alembic's, and only Alembic's (SPEC §6.1). An app that creates its own tables
will happily start against a database it has silently mis-migrated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from webhook_receiver.adapters.database import create_engine, create_session_factory
from webhook_receiver.api import health, webhooks
from webhook_receiver.api.middleware import CorrelationIdMiddleware
from webhook_receiver.api.state import STATE_ATTR, AppState
from webhook_receiver.config import Settings, get_settings
from webhook_receiver.obs.logging import configure_logging

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. `settings` is injectable so tests need no environment."""
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, environment=resolved.environment)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved)
        state = AppState(
            settings=resolved,
            engine=engine,
            session_factory=create_session_factory(engine),
        )
        setattr(app.state, STATE_ATTR, state)

        # The engine connects lazily, so startup does not fail on a database
        # that is merely slow to come up. `/readyz` reports the truth until it
        # does; that is precisely the split between liveness and readiness.
        log.info("app.started", environment=resolved.environment.value)
        try:
            yield
        finally:
            await engine.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title="Idempotent Webhook Receiver",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Outermost so every log line -- including a rejection before the endpoint
    # runs -- carries the correlation id (NFR-5).
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    return app
