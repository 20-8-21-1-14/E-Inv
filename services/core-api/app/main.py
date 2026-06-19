import uuid
import structlog
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from einv_common.config import settings
from einv_common.db import check_db, engine
from einv_common.storage import get_storage_client

from app.routers import documents, exports, webhooks

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("core-api starting", environment=settings.environment)
    await get_storage_client().ensure_buckets()
    yield
    await engine.dispose()
    logger.info("core-api shutdown")


app = FastAPI(
    title="e-Invoice OCR — Core API",
    version="0.1.0",
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.environment == "development" else [],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach X-Request-ID to every request and response for tracing."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    structlog.contextvars.bind_contextvars(request_id=request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    structlog.contextvars.clear_contextvars()
    return response


# ── Metrics ───────────────────────────────────────────────────────────────────

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health/live", tags=["health"], include_in_schema=False)
async def liveness():
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"], include_in_schema=False)
async def readiness():
    await check_db()
    return {"status": "ok"}


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(documents.router, prefix="/v1/documents", tags=["documents"])
app.include_router(exports.router, prefix="/v1/documents", tags=["exports"])
app.include_router(webhooks.router, prefix="/v1/webhooks", tags=["webhooks"])


# ── Global exception handler ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", error=str(exc), path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
    )
