"""FastAPI app factory. Run with `uvicorn hub.main:create_app --factory`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hub.api.auth import router as auth_router
from hub.api.console import router as console_router
from hub.api.console_drafts import router as console_drafts_router
from hub.api.demo import router as demo_router
from hub.api.errors import install_error_handlers
from hub.api.health import router as health_router
from hub.api.limits import BodySizeLimitMiddleware
from hub.api.middleware import RequestContextMiddleware
from hub.api.tickets import router as tickets_router
from hub.config import load_settings
from hub.deps import Container, build_container
from hub.observability.logging import configure_logging

# Longest run: triage + write timeouts with retries are longer, but a stop must not hang.
RUN_SHUTDOWN_WAIT_S = 30.0


def create_app(container: Container | None = None) -> FastAPI:
    """Build the app. Without a container, settings are read now, so a bad `.env` stops start."""
    if container is None:
        container = build_container(load_settings())
    configure_logging(container.settings.log_level)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        # Runs are tasks of their own (`hub.graph.linear.RunTasks`): let the ones in flight finish
        # before the database goes away. A run cut off here stays `running` for `recover-runs`.
        await container.runs.wait(timeout_s=RUN_SHUTDOWN_WAIT_S)
        if container.database is not None:
            await container.database.dispose()

    # debug stays off: Starlette's debug pages would show tracebacks with request data.
    app = FastAPI(
        title="Enterprise AI Support Hub", version="0.1.0", debug=False, lifespan=lifespan
    )
    app.state.container = container
    install_error_handlers(app)
    # The last one added runs first: the request id exists before the body cap can answer 413.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(demo_router)
    app.include_router(tickets_router)
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(console_drafts_router)
    return app
