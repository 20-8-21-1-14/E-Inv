import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from einv_common.config import settings
from einv_common.db import check_db, engine

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("admin-api starting", environment=settings.environment)
    yield
    await engine.dispose()
    logger.info("admin-api shutdown")


app = FastAPI(
    title="e-Invoice OCR — Admin API",
    version="0.1.0",
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.get("/health/live", tags=["health"])
async def liveness():
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness():
    await check_db()
    return {"status": "ok"}


# Routers registered in Stage 6
# from app.routers import hitl, tenants, system, users, auth
# app.include_router(auth.router, prefix="/admin/auth", tags=["auth"])
