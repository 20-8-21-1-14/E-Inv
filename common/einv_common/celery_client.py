"""Lightweight Celery client for dispatching tasks from non-worker services.

Imports this instead of the full ocr-worker celery_app so core-api
doesn't pull in PaddleOCR, OpenCV, etc.
"""

from celery import Celery
from einv_common.config import settings

_app: Celery | None = None


def get_celery_app() -> Celery:
    global _app
    if _app is None:
        _app = Celery("einv-client")
        _app.conf.update(
            broker_url=settings.celery_broker_url,
            result_backend=settings.celery_result_backend,
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
        )
    return _app
