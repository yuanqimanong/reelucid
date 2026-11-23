"""Assemble the HTTP application; resources start and stop in its lifespan."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI

from reelucid import __version__
from reelucid.api import admin, auth, components, events, jobs, media, system
from reelucid.api.http import BodyLimit, install_error_handlers, mount_web
from reelucid.api.runtime import lifespan
from reelucid.component_jobs import ComponentJobs
from reelucid.settings import AppConfig


def create_app(config: AppConfig | None = None) -> FastAPI:
    runtime = config or AppConfig()
    app = FastAPI(title="Reelucid · 帧闻录", version=__version__, lifespan=lifespan)
    app.state.config = runtime
    app.state.component_jobs = ComponentJobs(runtime.data_dir / "db/component-jobs.json")
    app.add_middleware(BodyLimit, config=runtime)
    install_error_handlers(app)
    app.include_router(auth.login_router, prefix="/api/v1")
    app.include_router(system.health_router, prefix="/api/v1")
    app.include_router(components.agent_router, prefix="/api/v1")

    private = APIRouter(prefix="/api/v1", dependencies=[Depends(auth.authenticated)])
    for router in (
        auth.session_router,
        system.system_router,
        media.asset_router,
        jobs.router,
        events.router,
        media.artifact_router,
        admin.router,
        components.router,
    ):
        private.include_router(router)
    app.include_router(private)
    # Register the SPA catch-all last so API routes take precedence.
    mount_web(app, runtime.web_dir)
    return app
