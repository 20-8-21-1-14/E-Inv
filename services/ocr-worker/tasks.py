"""Celery task entry points.

Each task calls asyncio.run() — owns its own event loop.
All async pipeline logic lives in pipeline/.
"""

import asyncio
import structlog

from celery_app import app
from einv_common.exceptions import RetryableError, NonRetryableError

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
    except Exception as exc:
        log.error("task.failed.unexpected", error=str(exc), exc_type=type(exc).__name__)
        raise NonRetryableError(str(exc)) from exc


async def _run_pipeline(document_id: str, tenant_id: str) -> dict:
    from pipeline.orchestrator import run
    return await run(document_id, tenant_id)


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
