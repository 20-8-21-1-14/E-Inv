from celery import Celery
from einv_common.config import settings

app = Celery("ocr-worker")

app.conf.update(
    broker_url=settings.celery_broker_url,
    result_backend=settings.celery_result_backend,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Ho_Chi_Minh",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,         # re-queue on worker crash
    worker_prefetch_multiplier=1,  # one task at a time per worker
    task_routes={
        "tasks.process_document": {"queue": "ocr"},
        "tasks.export_corrections": {"queue": "beat"},
    },
    beat_schedule={
        "export-hitl-corrections-nightly": {
            "task": "tasks.export_corrections",
            "schedule": 86400,  # every 24h
        },
    },
)

app.autodiscover_tasks(["tasks"])
