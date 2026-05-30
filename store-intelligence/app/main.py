"""
FastAPI entrypoint — wires all routers, configures structlog, and sets up
graceful startup/shutdown.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import anomalies, funnel, health, heatmap, ingestion, metrics
from app.db import get_db, init_db

# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup", message="Store Intelligence API starting")
    # Initialise the database tables on boot
    try:
        async with get_db() as db:
            await init_db(db)
        logger.info("startup", message="Database initialised")
    except Exception as exc:
        logger.warning("startup_db_warn", error=str(exc))
    yield
    logger.info("shutdown", message="Store Intelligence API shutting down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title="Store Intelligence API",
        version="1.0.0",
        description="Apex Retail — offline store analytics from CCTV footage",
        lifespan=lifespan,
    )

    # Routers
    app.include_router(ingestion.router, tags=["ingestion"])
    app.include_router(metrics.router, tags=["metrics"])
    app.include_router(funnel.router, tags=["funnel"])
    app.include_router(heatmap.router, tags=["heatmap"])
    app.include_router(anomalies.router, tags=["anomalies"])
    app.include_router(health.router, tags=["health"])

    # -----------------------------------------------------------------------
    # Global exception handler — no raw tracebacks in responses
    # -----------------------------------------------------------------------

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            error=type(exc).__name__,
            detail=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error"},
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
