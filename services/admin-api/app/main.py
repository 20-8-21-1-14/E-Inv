import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from einv_common.config import settings
from einv_common.db import check_db, engine
from app.routers import auth, corrections, models_admin, review, schemas_admin, tenants

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

app.include_router(auth.router,           prefix="/admin/auth",              tags=["auth"])
app.include_router(tenants.router,        prefix="/admin/tenants",           tags=["tenants"])
app.include_router(review.router,         prefix="/admin/review-queue",      tags=["hitl"])
app.include_router(corrections.router,    prefix="/admin/field-corrections",  tags=["hitl"])
app.include_router(schemas_admin.router,  prefix="/admin",                   tags=["schemas"])
app.include_router(models_admin.router,   prefix="/admin/models",            tags=["models"])


@app.get("/health/live", tags=["health"])
async def liveness():
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness():
    await check_db()
    return {"status": "ok"}
