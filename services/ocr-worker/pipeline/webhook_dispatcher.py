"""Non-blocking webhook delivery. Fire-and-forget — never fails document processing."""

import asyncio
from datetime import datetime, timezone

import httpx
import structlog

logger = structlog.get_logger()

_TIMEOUT = 10.0
_RETRIES = 3
_RETRY_DELAY = 2.0


async def dispatch(
    webhook_url: str,
    document_id: str,
    tenant_id: str,
    status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
) -> None:
    payload = {
        "event": "document.processed",
        "document_id": document_id,
        "tenant_id": tenant_id,
        "status": status,
        "confidence_score": confidence_score,
        "ocr_engine": ocr_engine,
        "result_url": f"/v1/documents/{document_id}/result",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for attempt in range(1, _RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            logger.info("webhook.delivered", document_id=document_id, attempt=attempt)
            return
        except Exception as exc:
            logger.warning("webhook.failed", document_id=document_id, attempt=attempt, error=str(exc))
            if attempt < _RETRIES:
                await asyncio.sleep(_RETRY_DELAY * attempt)


def fire_and_forget(
    webhook_url: str,
    document_id: str,
    tenant_id: str,
    status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
) -> None:
    """Schedule webhook delivery as a background coroutine — does not block."""
    asyncio.ensure_future(
        dispatch(webhook_url, document_id, tenant_id, status, confidence_score, ocr_engine)
    )
