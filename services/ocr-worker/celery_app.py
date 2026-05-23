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
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "tasks.process_document":   {"queue": "ocr"},
        "tasks.export_corrections": {"queue": "beat"},
        "tasks.check_drift":        {"queue": "beat"},
    },
    beat_schedule={
        # Nightly: export HITL corrections → MinIO training bucket
        "export-hitl-corrections-nightly": {
            "task": "tasks.export_corrections",
            "schedule": 86_400,    # 24 h
        },
        # Weekly: compare 7-day vs 30-day confidence; alert on drift
        "check-confidence-drift-weekly": {
            "task": "tasks.check_drift",
            "schedule": 604_800,   # 7 days
        },
    },
)

app.autodiscover_tasks(["tasks"])
