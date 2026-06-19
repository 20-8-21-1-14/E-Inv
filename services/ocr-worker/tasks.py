"""Celery task entry points.

Each task calls asyncio.run() — owns its own event loop.
All async pipeline logic lives in pipeline/.
"""

import asyncio
import structlog

from celery_app import app
from einv_common.exceptions import NonRetryableError, RetryableError

# Low-level errors that indicate transient infrastructure problems (should retry)
_RETRYABLE_EXC = (OSError, ConnectionError, TimeoutError)

logger = structlog.get_logger()


@app.task(
    bind=True,
    name="tasks.process_document",
    max_retries=3,
    default_retry_delay=10,
    autoretry_for=(RetryableError,),
    retry_backoff=True,
    retry_backoff_max=300,
    dont_autoretry_for=(NonRetryableError,),
)
def process_document(self, document_id: str, tenant_id: str) -> dict:
    """Main OCR processing task. Runs the full pipeline for one document."""
    log = logger.bind(document_id=document_id, tenant_id=tenant_id, task_id=self.request.id)
    log.info("task.started")
    try:
        result = asyncio.run(_run_pipeline(document_id, tenant_id))
        log.info("task.completed", status=result["status"])
        return result
    except NonRetryableError as exc:
        log.error("task.failed.permanent", error=str(exc), exc_type=type(exc).__name__)
        raise
    except RetryableError as exc:
        log.warning("task.failed.retryable", error=str(exc), retries=self.request.retries)
        raise
    except _RETRYABLE_EXC as exc:
        log.warning("task.failed.retryable_infra", error=str(exc), exc_type=type(exc).__name__)
        raise RetryableError(str(exc)) from exc
    except Exception as exc:
        log.error("task.failed.unexpected", error=str(exc), exc_type=type(exc).__name__)
        raise NonRetryableError(str(exc)) from exc


async def _run_pipeline(document_id: str, tenant_id: str) -> dict:
    from pipeline.orchestrator import run
    return await run(document_id, tenant_id)


class _ShouldRetry(Exception):
    pass


class _ShouldAbandon(Exception):
    pass


@app.task(
    bind=True,
    name="tasks.deliver_webhook",
    max_retries=5,
    dont_autoretry_for=(Exception,),  # manual retry with custom backoff schedule
)
def deliver_webhook(
    self,
    delivery_id: str,
    document_id: str,
    tenant_id: str,
    event_type: str,
    final_status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
) -> dict:
    """Fire a signed webhook POST to the tenant's webhook_url with exponential backoff."""
    log = logger.bind(delivery_id=delivery_id, document_id=document_id, attempt=self.request.retries + 1)
    log.info("webhook_task.started")
    try:
        result = asyncio.run(
            _run_deliver_webhook(
                delivery_id=delivery_id,
                document_id=document_id,
                tenant_id=tenant_id,
                event_type=event_type,
                final_status=final_status,
                confidence_score=confidence_score,
                ocr_engine=ocr_engine,
                attempt=self.request.retries + 1,
            )
        )
        log.info("webhook_task.completed", delivered=result.get("delivered"))
        return result
    except _ShouldRetry as exc:
        attempt = self.request.retries + 1
        if attempt >= self.max_retries:
            log.error("webhook_task.abandoned", error=str(exc))
            asyncio.run(_write_abandoned_row(delivery_id, document_id, tenant_id, event_type, attempt))
            return {"delivered": False, "abandoned": True}
        delay = 2 ** attempt  # 2, 4, 8, 16, 32 seconds
        log.warning("webhook_task.retry", delay=delay, error=str(exc))
        raise self.retry(exc=exc, countdown=delay)
    except _ShouldAbandon as exc:
        attempt = self.request.retries + 1
        log.error("webhook_task.abandoned_non_retryable", error=str(exc))
        asyncio.run(_write_abandoned_row(delivery_id, document_id, tenant_id, event_type, attempt))
        return {"delivered": False, "abandoned": True}


async def _run_deliver_webhook(
    *,
    delivery_id: str,
    document_id: str,
    tenant_id: str,
    event_type: str,
    final_status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
    attempt: int,
) -> dict:
    import hashlib
    import hmac
    import json
    import time
    import uuid as _uuid
    import httpx
    from datetime import datetime, timezone
    from einv_common.db import session_factory
    from einv_common.models.tenant import Tenant
    from einv_common.models.webhook import WebhookDelivery
    from pipeline.webhook_dispatcher import validate_webhook_url
    from sqlalchemy import select

    async with session_factory() as session:
        tenant_row = await session.execute(select(Tenant).where(Tenant.id == _uuid.UUID(tenant_id)))
        tenant = tenant_row.scalar_one_or_none()

        if tenant is None or not tenant.webhook_url:
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt, status="abandoned", http_status=None,
                duration_ms=None, response_body="No webhook_url configured", next_retry_at=None,
            )
            return {"delivered": False, "skipped": True}

        try:
            validate_webhook_url(tenant.webhook_url)
        except ValueError as exc:
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt, status="abandoned", http_status=None,
                duration_ms=None, response_body=str(exc), next_retry_at=None,
            )
            raise _ShouldAbandon(str(exc)) from exc

        timestamp = int(time.time())
        payload = {
            "event": event_type,
            "event_id": delivery_id,
            "api_version": "2026-06-19",
            "tenant_id": tenant_id,
            "document_id": document_id,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "status": final_status,
                "confidence_score": confidence_score,
                "ocr_engine": ocr_engine,
                "result_url": f"/v1/documents/{document_id}/result",
            },
        }
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()

        # Sign with raw secret — consumers verify using the raw secret shown at tenant creation
        secret = tenant.webhook_secret or ""
        sig = hmac.new(secret.encode(), f"{timestamp}.".encode() + body_bytes, hashlib.sha256).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-EInvoice-Signature": sig,
            "X-EInvoice-Timestamp": str(timestamp),
            "X-EInvoice-Event-Id": delivery_id,
        }

        start_ms = int(time.monotonic() * 1000)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=10.0),
                follow_redirects=False,
            ) as client:
                resp = await client.post(tenant.webhook_url, content=body_bytes, headers=headers)
            duration_ms = int(time.monotonic() * 1000) - start_ms
            response_body = resp.text[:500] if resp.text else None

            # Non-retryable 4xx (except 408 Request Timeout, 429 Too Many Requests)
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                await _write_delivery_row(
                    session, delivery_id, document_id, tenant_id, event_type,
                    attempt=attempt, status="abandoned", http_status=resp.status_code,
                    duration_ms=duration_ms, response_body=response_body, next_retry_at=None,
                )
                raise _ShouldAbandon(f"Non-retryable HTTP {resp.status_code}")

            success = resp.is_success
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt,
                status="success" if success else "failed",
                http_status=resp.status_code,
                duration_ms=duration_ms,
                response_body=response_body,
                next_retry_at=None,
                delivered_at=datetime.now(timezone.utc) if success else None,
            )
            if not success:
                raise _ShouldRetry(f"HTTP {resp.status_code}")
            return {"delivered": True, "http_status": resp.status_code}

        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            duration_ms = int(time.monotonic() * 1000) - start_ms
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt, status="failed", http_status=None,
                duration_ms=duration_ms, response_body=str(exc)[:500], next_retry_at=None,
            )
            raise _ShouldRetry(str(exc)) from exc


async def _write_delivery_row(
    session,
    delivery_id: str,
    document_id: str,
    tenant_id: str,
    event_type: str,
    *,
    attempt: int,
    status: str,
    http_status: int | None,
    duration_ms: int | None,
    response_body: str | None,
    next_retry_at,
    delivered_at=None,
) -> None:
    import uuid as _uuid
    from einv_common.models.webhook import WebhookDelivery

    session.add(WebhookDelivery(
        id=_uuid.uuid4(),          # unique PK per attempt row
        event_id=delivery_id,      # stable across retries; used as payload event_id
        document_id=_uuid.UUID(document_id),
        tenant_id=_uuid.UUID(tenant_id),
        event_type=event_type,
        attempt=attempt,
        http_status=http_status,
        status=status,
        duration_ms=duration_ms,
        response_body_truncated=response_body,
        next_retry_at=next_retry_at,
        delivered_at=delivered_at,
    ))
    await session.commit()


async def _write_abandoned_row(
    delivery_id: str,
    document_id: str,
    tenant_id: str,
    event_type: str,
    attempt: int,
) -> None:
    """Write a final abandoned row when the task exhausts all Celery retries."""
    from einv_common.db import session_factory
    async with session_factory() as session:
        await _write_delivery_row(
            session, delivery_id, document_id, tenant_id, event_type,
            attempt=attempt, status="abandoned", http_status=None,
            duration_ms=None, response_body="Max retries exhausted", next_retry_at=None,
        )


@app.task(name="tasks.export_corrections")
def export_corrections() -> dict:
    """Nightly: export HITL corrections to training annotation format."""
    count = asyncio.run(_run_export())
    return {"exported": count}


async def _run_export() -> int:
    from pipeline.feedback_exporter import export_corrections as do_export
    return await do_export()


@app.task(name="tasks.check_drift")
def check_drift() -> dict:
    """Weekly: compare 7-day vs 30-day confidence average and alert on drift."""
    return asyncio.run(_run_drift_check())


async def _run_drift_check() -> dict:
    from pipeline.drift_monitor import check_drift as do_check
    return await do_check()
