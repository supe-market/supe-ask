"""FastAPI application factory for the supe-ask control plane.

The app is intentionally thin. It runs the SQL migrations, starts the ECS
execution reconciler, then delegates Ask behavior to the routed services below.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from .config import settings
from .migration_runner import run_migrations
from .routes.api import router as api_router
from .services.llm import llm_service
from .services.reconciler import execution_reconciler

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the supe-ask FastAPI application."""
    run_migrations()
    app = FastAPI(default_response_class=ORJSONResponse, title=settings.app_name)
    app.state.ready = False
    app.state.readiness_reason = "Startup validation has not completed"
    allow_origin_regex = settings.allowed_origins if settings.allowed_origins == ".*" else None
    allow_origins = [] if allow_origin_regex else [item.strip() for item in settings.allowed_origins.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def healthcheck():
        return {"success": True, "service": settings.app_name}

    @app.get("/health/ready")
    def readiness():
        payload = {
            "success": bool(app.state.ready),
            "service": settings.app_name,
            "provider": settings.ask_llm_provider,
            "reason": app.state.readiness_reason,
        }
        return ORJSONResponse(payload, status_code=200 if app.state.ready else 503)

    @app.on_event("startup")
    def startup_tasks() -> None:
        logger.info("Validating supe-ask startup dependencies", extra={"provider": settings.ask_llm_provider})
        llm_service.validate_provider()
        app.state.ready = True
        app.state.readiness_reason = ""
        execution_reconciler.start()

    @app.on_event("shutdown")
    def shutdown_tasks() -> None:
        app.state.ready = False
        app.state.readiness_reason = "Service is shutting down"
        execution_reconciler.stop()

    app.include_router(api_router)
    return app
