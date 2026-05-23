"""Webhook test endpoint."""

import structlog
import httpx
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from einv_common.models import Tenant
from app.dependencies import get_current_tenant

logger = structlog.get_logger()
router = APIRouter()

_WEBHOOK_TIMEOUT = 10.0  # seconds


@router.post("/test", summary="Fire a test webhook to the tenant's configured webhook_url")
async def test_webhook(tenant: Tenant = Depends(get_current_tenant)) -> dict:
    if not tenant.webhook_url:
        raise HTTPException(
            422,
            detail={
                "code": "NO_WEBHOOK_URL",
                "message": "No webhook_url configured for this tenant",
            },
        )

    payload = {
        "event": "webhook.test",
        "tenant_id": str(tenant.id),
        "status": "test",
        "confidence_score": None,
        "ocr_engine": None,
        "result_url": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
            resp = await client.post(tenant.webhook_url, json=payload)
        logger.info("webhook.test.sent", tenant_id=str(tenant.id), status_code=resp.status_code)
        return {
            "delivered": resp.is_success,
            "http_status": resp.status_code,
            "webhook_url": tenant.webhook_url,
        }
    except httpx.TimeoutException:
        raise HTTPException(
            504,
            detail={"code": "WEBHOOK_TIMEOUT", "message": f"No response within {_WEBHOOK_TIMEOUT}s"},
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            502,
            detail={"code": "WEBHOOK_UNREACHABLE", "message": str(exc)},
        )
