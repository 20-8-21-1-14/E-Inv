"""Confidence drift detector.

Runs as a Celery beat task (weekly by default). Compares the rolling 7-day
average confidence score against the 30-day baseline. If the gap exceeds
DRIFT_THRESHOLD, emits a structured warning log that Prometheus/Grafana
can alert on, and creates a HitlQueue entry so admins are notified in-app.

Drift signals that:
  - New invoice templates are appearing that the model hasn't seen
  - OCR print quality at certain clients has degraded
  - Label schema needs updating (new column variants)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select

from einv_common.db import session_factory
from einv_common.models.extraction import ExtractionResult

logger = structlog.get_logger()

_DRIFT_THRESHOLD   = float(os.environ.get("DRIFT_THRESHOLD", "0.05"))    # 5% drop triggers alert
_WINDOW_SHORT_DAYS = int(os.environ.get("DRIFT_WINDOW_SHORT", "7"))
_WINDOW_LONG_DAYS  = int(os.environ.get("DRIFT_WINDOW_LONG", "30"))
_MIN_SAMPLES       = int(os.environ.get("DRIFT_MIN_SAMPLES", "50"))      # ignore if too few docs


async def check_drift() -> dict:
    """Compute confidence averages and detect drift.

    Returns:
        {
          "short_avg": float,
          "long_avg":  float,
          "drop":      float,   # positive = degradation
          "drift_detected": bool,
          "short_samples": int,
          "long_samples":  int,
        }
    """
    now = datetime.now(timezone.utc)
    short_start = now - timedelta(days=_WINDOW_SHORT_DAYS)
    long_start  = now - timedelta(days=_WINDOW_LONG_DAYS)

    async with session_factory() as session:
        short_row = await session.execute(
            select(
                func.avg(ExtractionResult.confidence_score).label("avg"),
                func.count().label("cnt"),
            ).where(ExtractionResult.created_at >= short_start)
        )
        short = short_row.one()

        long_row = await session.execute(
            select(
                func.avg(ExtractionResult.confidence_score).label("avg"),
                func.count().label("cnt"),
            ).where(
                ExtractionResult.created_at >= long_start,
                ExtractionResult.created_at < short_start,
            )
        )
        long = long_row.one()

    short_avg = float(short.avg or 0)
    short_cnt = int(short.cnt or 0)
    long_avg  = float(long.avg or 0)
    long_cnt  = int(long.cnt or 0)

    drift_detected = False
    drop = 0.0

    if short_cnt >= _MIN_SAMPLES and long_cnt >= _MIN_SAMPLES and long_avg > 0:
        drop = long_avg - short_avg   # positive means current period is worse
        drift_detected = drop > _DRIFT_THRESHOLD

    result = {
        "short_avg":      round(short_avg, 4),
        "long_avg":       round(long_avg, 4),
        "drop":           round(drop, 4),
        "drift_detected": drift_detected,
        "short_samples":  short_cnt,
        "long_samples":   long_cnt,
        "threshold":      _DRIFT_THRESHOLD,
    }

    if drift_detected:
        logger.warning(
            "drift_monitor.drift_detected",
            **result,
            action="check_new_invoice_templates_and_schema",
        )
        await _create_hitl_drift_alert(result)
    else:
        logger.info("drift_monitor.ok", **result)

    return result


async def _create_hitl_drift_alert(stats: dict) -> None:
    """Create a placeholder HitlQueue entry to alert admins of drift in-app."""
    from einv_common.models.hitl import HitlQueue
    from einv_common.models.document import Document
    from sqlalchemy import select

    async with session_factory() as session:
        # Find the most recently processed document to attach the alert to
        result = await session.execute(
            select(Document)
            .where(Document.status == "done")
            .order_by(Document.processed_at.desc())
            .limit(1)
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return

        session.add(HitlQueue(
            document_id=doc.id,
            tenant_id=doc.tenant_id,
            reason="manual_flag",
            notes=(
                f"DRIFT ALERT: 7-day avg confidence={stats['short_avg']:.3f} "
                f"vs 30-day baseline={stats['long_avg']:.3f} "
                f"(drop={stats['drop']:.3f} > threshold={stats['threshold']}). "
                f"Possible cause: new invoice template or OCR quality degradation. "
                f"Check ColumnAliasProposals for unmatched headers."
            ),
        ))
        await session.commit()
