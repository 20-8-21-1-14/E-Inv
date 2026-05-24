"""Export HITL corrections for OCR model retraining.

Called nightly by a Celery beat task via tasks.export_corrections().
Queries FieldConfidence and InvoiceLineItem records that have been corrected
by a human reviewer (is_corrected=True, corrected_value not null, exported=False).

Output: PPOCRLabel-compatible JSON Lines file uploaded to MinIO under:
  s3://<training-bucket>/exports/YYYY-MM-DD/corrections.jsonl

Each line:
  {"field": "item_name", "original": "...", "corrected": "...", "confidence": 0.72}

After export, records are marked exported=True.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.config import settings
from einv_common.db import session_factory
from einv_common.models.extraction import FieldConfidence, InvoiceLineItem

logger = structlog.get_logger()
_BATCH_LIMIT = 5000


async def export_corrections(export_date: date | None = None) -> int:
    """Export all un-exported HITL corrections.

    Returns:
        Number of records exported.
    """
    if export_date is None:
        export_date = date.today()

    async with session_factory() as session:
        field_records  = await _fetch_field_corrections(session)
        item_records   = await _fetch_line_item_corrections(session)

        total = len(field_records) + len(item_records)
        if total == 0:
            logger.info("feedback_exporter.nothing_to_export")
            return 0

        lines = (
            _build_field_lines(field_records)
            + _build_item_lines(item_records)
        )

        # Upload first — if upload fails we do NOT mark as exported, so next
        # nightly run will retry the same records cleanly.
        await _upload(lines, export_date)

        field_ids = [r.id for r in field_records]
        item_ids  = [r.id for r in item_records]
        try:
            await _mark_field_exported(session, field_ids)
            await _mark_item_exported(session, item_ids)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("feedback_exporter.mark_exported_failed",
                             field_count=len(field_ids), item_count=len(item_ids))
            raise

    logger.info("feedback_exporter.done", exported=total, date=str(export_date))
    return total


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def _fetch_field_corrections(session: AsyncSession) -> list[FieldConfidence]:
    stmt = (
        select(FieldConfidence)
        .where(FieldConfidence.is_corrected == True)    # noqa: E712
        .where(FieldConfidence.corrected_value.is_not(None))
        .where(FieldConfidence.exported == False)       # noqa: E712
        .limit(_BATCH_LIMIT)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _fetch_line_item_corrections(session: AsyncSession) -> list[InvoiceLineItem]:
    stmt = (
        select(InvoiceLineItem)
        .where(InvoiceLineItem.is_corrected == True)    # noqa: E712
        .where(InvoiceLineItem.exported == False)       # noqa: E712
        .limit(_BATCH_LIMIT)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Build export lines
# ---------------------------------------------------------------------------

def _build_field_lines(records: list[FieldConfidence]) -> list[str]:
    lines: list[str] = []
    for rec in records:
        entry = {
            "type": "field",
            "field": rec.field_name,
            "original": rec.raw_value,
            "corrected": rec.corrected_value,
            "confidence": rec.confidence,
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
    return lines


def _build_item_lines(records: list[InvoiceLineItem]) -> list[str]:
    lines: list[str] = []
    for rec in records:
        entry = {
            "type": "line_item",
            "item_name": rec.item_name,
            "unit": rec.unit,
            "quantity": str(rec.quantity),
            "unit_price": str(rec.unit_price),
            "amount": str(rec.amount),
            "tax_rate": str(rec.tax_rate),
            "corrected_by": rec.corrected_by,
            "corrected_at": rec.corrected_at.isoformat() if rec.corrected_at else None,
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
    return lines


# ---------------------------------------------------------------------------
# Upload + mark exported
# ---------------------------------------------------------------------------

async def _upload(lines: list[str], export_date: date) -> None:
    from einv_common.storage import get_storage_client

    content = "\n".join(lines).encode("utf-8")
    key = f"exports/{export_date}/corrections.jsonl"

    client = get_storage_client()
    await client.upload(
        bucket=settings.minio_bucket_training,
        key=key,
        data=content,
        content_type="application/jsonl",
    )
    logger.info("feedback_exporter.uploaded", key=key, lines=len(lines))


async def _mark_field_exported(session: AsyncSession, ids: list[UUID]) -> None:
    if not ids:
        return
    await session.execute(
        update(FieldConfidence)
        .where(FieldConfidence.id.in_(ids))
        .values(exported=True)
    )


async def _mark_item_exported(session: AsyncSession, ids: list[UUID]) -> None:
    if not ids:
        return
    await session.execute(
        update(InvoiceLineItem)
        .where(InvoiceLineItem.id.in_(ids))
        .values(exported=True)
    )
