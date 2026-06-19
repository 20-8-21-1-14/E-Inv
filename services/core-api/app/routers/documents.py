"""Document ingestion, status, result, and list endpoints."""

import hashlib
import math
import uuid
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import redis.asyncio as aioredis

from einv_common.celery_client import get_celery_app
from einv_common.config import settings
from einv_common.db import get_session
from einv_common.exceptions import UnsupportedFormatError
from einv_common.models import AuditLog, Document, ExtractionResult, Tenant
from einv_common.schemas import DocumentStatus, ExtractionResultOut
from einv_common.schemas.common import ErrorResponse, PaginatedResponse
from einv_common.storage import StorageClient

from app.dependencies import get_current_tenant, get_redis, get_storage
from app.ratelimit import check_quota

logger = structlog.get_logger()
router = APIRouter()

# ── Helpers ───────────────────────────────────────────────────────────────────

_SUPPORTED_TYPES: dict[str, str] = {
    "text/xml": "xml",
    "application/xml": "xml",
    "application/pdf": "pdf",
    "image/jpeg": "image",
    "image/png": "image",
    "image/tiff": "image",
    "image/webp": "image",
}

_FILE_EXT: dict[str, str] = {
    "xml": "xml",
    "pdf": "pdf",
    "image": "bin",  # actual ext resolved from content_type below
}

_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tif",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "text/xml": "xml",
    "application/xml": "xml",
}


def _detect_format(content: bytes, content_type: str) -> str:
    """Detect source format from magic bytes, falling back to MIME type."""
    if content[:5] in (b"<?xml", b"<e-in") or (len(content) > 0 and content[0:1] == b"<"):
        return "xml"
    if content[:4] == b"%PDF":
        return "pdf"
    # JPEG: FF D8 FF  |  PNG: 89 50 4E 47  |  TIFF: 49 49 or 4D 4D
    if content[:2] == b"\xff\xd8" or content[:4] == b"\x89PNG" or content[:2] in (b"II", b"MM"):
        return "image"
    # Fall back to MIME type
    fmt = _SUPPORTED_TYPES.get(content_type.split(";")[0].strip().lower())
    if fmt:
        return fmt
    raise UnsupportedFormatError(f"Unsupported content type: {content_type}")


def _storage_path(tenant_id: uuid.UUID, document_id: uuid.UUID, fmt: str, content_type: str) -> str:
    ext = _MIME_EXT.get(content_type.split(";")[0].strip().lower(), fmt)
    return f"{tenant_id}/{document_id}/original.{ext}"


# ── Idempotency ───────────────────────────────────────────────────────────────

_IDEMPOTENCY_TTL = 86_400  # 24 h


async def _check_idempotency(
    key: str | None, tenant_id: str, redis: aioredis.Redis
) -> dict | None:
    """Return cached response if key was seen in the last 24 h."""
    if not key:
        return None
    rkey = f"idempotency:{tenant_id}:{key}"
    cached = await redis.hgetall(rkey)
    if cached:
        return cached
    return None


async def _store_idempotency(
    key: str | None, tenant_id: str, redis: aioredis.Redis, response: dict
) -> None:
    if not key:
        return
    rkey = f"idempotency:{tenant_id}:{key}"
    await redis.hset(rkey, mapping=response)
    await redis.expire(rkey, _IDEMPOTENCY_TTL)


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post("/upload", status_code=202, summary="Upload invoice document for OCR processing")
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form(default="vat_invoice"),
    x_idempotency_key: Annotated[str | None, Header()] = None,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
    storage: StorageClient = Depends(get_storage),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    log = logger.bind(tenant_id=str(tenant.id), doc_type=doc_type)

    # ── Idempotency check ──────────────────────────────────────────────────
    cached = await _check_idempotency(x_idempotency_key, str(tenant.id), redis)
    if cached:
        log.info("upload.idempotent_hit", key=x_idempotency_key)
        return cached

    # ── Validate MIME type ─────────────────────────────────────────────────
    content_type = file.content_type or ""
    if content_type.split(";")[0].strip().lower() not in _SUPPORTED_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_MEDIA_TYPE",
                "message": f"Accepted: {', '.join(_SUPPORTED_TYPES)}",
            },
        )

    # ── Read file ──────────────────────────────────────────────────────────
    content = await file.read()
    size = len(content)
    if size == 0:
        raise HTTPException(400, detail={"code": "EMPTY_FILE", "message": "File must not be empty"})
    if size > settings.max_upload_size_bytes:
        raise HTTPException(
            413,
            detail={
                "code": "FILE_TOO_LARGE",
                "message": f"Max size is {settings.max_upload_size_mb} MB",
            },
        )

    # ── Format detection ───────────────────────────────────────────────────
    try:
        source_format = _detect_format(content, content_type)
    except UnsupportedFormatError as exc:
        raise HTTPException(415, detail={"code": "UNSUPPORTED_FORMAT", "message": str(exc)})

    # ── Quota check (after size/MIME validation — only valid uploads consume quota) ─
    await check_quota(tenant, redis)

    # ── Dedup by SHA-256 ───────────────────────────────────────────────────
    file_hash = hashlib.sha256(content).hexdigest()
    existing = await session.execute(
        select(Document).where(
            Document.tenant_id == tenant.id,
            Document.file_hash == file_hash,
            Document.status.in_(["queued", "processing", "done", "hitl"]),
        )
    )
    dup = existing.scalar_one_or_none()
    if dup is not None:
        log.info("upload.dedup_hit", document_id=str(dup.id))
        resp = {
            "document_id": str(dup.id),
            "task_id": dup.task_id or "",
            "status": dup.status,
            "deduplicated": "true",
        }
        await _store_idempotency(x_idempotency_key, str(tenant.id), redis, resp)
        return resp

    # ── Store raw file in MinIO ────────────────────────────────────────────
    document_id = uuid.uuid4()
    file_path = _storage_path(tenant.id, document_id, source_format, content_type)
    try:
        await storage.upload(settings.minio_bucket_raw, file_path, content, content_type)
    except Exception as exc:
        log.error("upload.storage_failed", error=str(exc))
        raise HTTPException(503, detail={"code": "STORAGE_ERROR", "message": "File storage unavailable"})

    # ── Persist document record ────────────────────────────────────────────
    document = Document(
        id=document_id,
        tenant_id=tenant.id,
        doc_type=doc_type,
        source_format=source_format,
        status="queued",
        file_path=file_path,
        file_hash=file_hash,
    )
    audit = AuditLog(
        tenant_id=tenant.id,
        document_id=document_id,
        action="upload",
        actor=f"api_key",
        details={"doc_type": doc_type, "source_format": source_format, "size_bytes": size},
    )
    session.add(document)
    session.add(audit)
    await session.commit()

    # ── Dispatch OCR task (after commit — document now readable by worker) ─
    celery = get_celery_app()
    task = celery.send_task(
        "tasks.process_document",
        args=[str(document_id), str(tenant.id)],
        queue="ocr",
    )

    # Update task_id
    await session.execute(
        update(Document).where(Document.id == document_id).values(task_id=str(task.id))
    )
    await session.commit()

    log.info("upload.accepted", document_id=str(document_id), task_id=str(task.id))
    resp = {
        "document_id": str(document_id),
        "task_id": str(task.id),
        "status": "queued",
        "deduplicated": "false",
    }
    await _store_idempotency(x_idempotency_key, str(tenant.id), redis, resp)
    return resp


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/{document_id}", summary="Get document processing status")
async def get_document(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    doc = await _get_owned_document(document_id, tenant.id, session)
    return {
        "document_id": str(doc.id),
        "tenant_id": str(doc.tenant_id),
        "doc_type": doc.doc_type,
        "source_format": doc.source_format,
        "status": doc.status,
        "task_id": doc.task_id,
        "created_at": doc.created_at.isoformat(),
        "processed_at": doc.processed_at.isoformat() if doc.processed_at else None,
        "error_message": doc.error_message,
    }


# ── Result ────────────────────────────────────────────────────────────────────

@router.get("/{document_id}/result", summary="Get full extraction result")
async def get_result(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    doc = await _get_owned_document(document_id, tenant.id, session)

    if doc.status not in ("done", "hitl"):
        raise HTTPException(
            404,
            detail={
                "code": "RESULT_NOT_READY",
                "message": f"Document status is '{doc.status}'. Result available when status is 'done' or 'hitl'.",
                "document_id": str(document_id),
            },
        )

    result_row = await session.execute(
        select(ExtractionResult)
        .where(ExtractionResult.document_id == document_id)
        .options(
            selectinload(ExtractionResult.line_items),
            selectinload(ExtractionResult.field_confidences),
        )
    )
    result = result_row.scalar_one_or_none()
    if result is None:
        raise HTTPException(404, detail={"code": "RESULT_NOT_FOUND", "document_id": str(document_id)})

    return {
        "document_id": str(document_id),
        "result_id": str(result.id),
        "confidence_score": result.confidence_score,
        "ocr_engine": result.ocr_engine,
        "processing_time_ms": result.processing_time_ms,
        "subtotal": str(result.subtotal) if result.subtotal is not None else None,
        "total_tax": str(result.total_tax) if result.total_tax is not None else None,
        "grand_total": str(result.grand_total) if result.grand_total is not None else None,
        "validated_fields": result.validated_fields,
        "validation_errors": result.validation_errors,
        "line_items": [
            {
                "id": str(li.id),
                "line_number": li.line_number,
                "item_name": li.item_name,
                "item_code": li.item_code,
                "unit": li.unit,
                "quantity": str(li.quantity),
                "unit_price": str(li.unit_price),
                "amount": str(li.amount),
                "discount_rate": str(li.discount_rate) if li.discount_rate is not None else None,
                "discount_amount": str(li.discount_amount) if li.discount_amount is not None else None,
                "tax_rate": str(li.tax_rate),
                "tax_amount": str(li.tax_amount),
                "total_amount": str(li.total_amount),
                "name_confidence": li.name_confidence,
                "qty_confidence": li.qty_confidence,
                "unit_confidence": li.unit_confidence,
                "price_confidence": li.price_confidence,
                "is_corrected": li.is_corrected,
            }
            for li in sorted(result.line_items, key=lambda x: x.line_number)
        ],
        "field_confidences": [
            {
                "field_name": fc.field_name,
                "raw_value": fc.raw_value,
                "confidence": fc.confidence,
                "is_corrected": fc.is_corrected,
                "corrected_value": fc.corrected_value,
            }
            for fc in result.field_confidences
        ],
    }


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/", summary="List documents with filters")
async def list_documents(
    status: str | None = Query(default=None),
    doc_type: str | None = Query(default=None),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    base = select(Document).where(Document.tenant_id == tenant.id)

    if status:
        base = base.where(Document.status == status)
    if doc_type:
        base = base.where(Document.doc_type == doc_type)
    if from_date:
        base = base.where(Document.created_at >= from_date)
    if to_date:
        base = base.where(Document.created_at <= to_date)

    total_result = await session.execute(select(func.count()).select_from(base.subquery()))
    total: int = total_result.scalar_one()

    docs_result = await session.execute(
        base.order_by(Document.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    docs = docs_result.scalars().all()

    return {
        "items": [
            {
                "document_id": str(d.id),
                "doc_type": d.doc_type,
                "source_format": d.source_format,
                "status": d.status,
                "created_at": d.created_at.isoformat(),
                "processed_at": d.processed_at.isoformat() if d.processed_at else None,
            }
            for d in docs
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": math.ceil(total / limit) if total else 0,
    }


# ── Shared helper ─────────────────────────────────────────────────────────────

async def _get_owned_document(
    document_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> Document:
    result = await session.execute(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(
            404,
            detail={"code": "DOCUMENT_NOT_FOUND", "document_id": str(document_id)},
        )
    return doc


# ── Retry ─────────────────────────────────────────────────────────────────────

_MAX_RETRIES = 3


@router.post("/{document_id}/retry", status_code=202, summary="Re-queue a failed document for processing")
async def retry_document(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    doc = await _get_owned_document(document_id, tenant.id, session)

    if doc.processing_attempts >= _MAX_RETRIES:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "MAX_RETRIES_REACHED",
                "message": f"Document has been retried {doc.processing_attempts} times (max {_MAX_RETRIES}).",
            },
        )

    if doc.status != "error":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NOT_IN_ERROR_STATE",
                "message": f"Document status is '{doc.status}'. Retry only allowed when status is 'error'.",
            },
        )

    # Atomic update — guards status='error' AND attempts < max in the WHERE to prevent TOCTOU races
    result = await session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant.id,
            Document.status == "error",
            Document.processing_attempts < _MAX_RETRIES,
        )
        .values(
            status="queued",
            error_message=None,
            processing_attempts=Document.processing_attempts + 1,
            last_retry_at=func.now(),
        )
        .returning(Document.id, Document.processing_attempts)
    )
    row = result.one_or_none()

    if row is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NOT_IN_ERROR_STATE",
                "message": "Document is no longer in 'error' state or max retries reached.",
            },
        )

    _, attempt_number = row

    session.add(AuditLog(
        tenant_id=tenant.id,
        document_id=document_id,
        action="retry",
        actor="api_key",
        details={"attempt": attempt_number},
    ))
    await session.commit()

    celery = get_celery_app()
    task = celery.send_task(
        "tasks.process_document",
        args=[str(document_id), str(tenant.id)],
        queue="ocr",
    )

    logger.info("document.retry.queued", document_id=str(document_id), attempt=attempt_number, task_id=str(task.id))
    return {
        "document_id": str(document_id),
        "task_id": str(task.id),
        "status": "queued",
        "attempt": attempt_number,
    }
