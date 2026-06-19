"""CSV and JSON export endpoints for extracted invoice data."""

import csv
import io
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from einv_common.db import get_session
from einv_common.models import Document, ExtractionResult
from einv_common.models.tenant import Tenant
from app.dependencies import get_current_tenant

logger = structlog.get_logger()
router = APIRouter()

_READY_STATUSES = ("done", "hitl")

_CSV_HEADER = [
    "invoice_number", "invoice_date", "seller_tax_id", "buyer_tax_id",
    "seller_name", "buyer_name", "grand_total", "currency",
    "line_number", "item_name", "item_code", "unit",
    "quantity", "unit_price", "tax_rate", "tax_amount", "total_amount",
]


def _safe_cell(value: str | None) -> str:
    """Prefix values that Excel would interpret as formulas."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


async def _load_result(
    document_id: uuid.UUID,
    tenant: Tenant,
    session: AsyncSession,
) -> tuple[Document, ExtractionResult]:
    doc_row = await session.execute(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant.id,
        )
    )
    doc = doc_row.scalar_one_or_none()
    if doc is None:
        raise HTTPException(404, detail={"code": "DOCUMENT_NOT_FOUND"})
    if doc.status not in _READY_STATUSES:
        raise HTTPException(
            404,
            detail={
                "code": "RESULT_NOT_READY",
                "message": (
                    f"Document status is '{doc.status}'. "
                    "Export available when status is 'done' or 'hitl'."
                ),
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
        raise HTTPException(404, detail={"code": "RESULT_NOT_FOUND"})

    return doc, result


@router.get("/{document_id}/export/csv", summary="Export extraction result as CSV")
async def export_csv(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    doc, result = await _load_result(document_id, tenant, session)
    vf = result.validated_fields

    def _gen():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_HEADER)
        line_items = sorted(result.line_items, key=lambda x: x.line_number)

        if not line_items:
            # Emit one row with header/totals and empty line-item columns
            writer.writerow([
                _safe_cell(vf.get("invoice_number")),
                _safe_cell(vf.get("invoice_date")),
                _safe_cell(vf.get("seller_tax_code")),
                _safe_cell(vf.get("buyer_tax_code")),
                _safe_cell(vf.get("seller_name")),
                _safe_cell(vf.get("buyer_name")),
                _safe_cell(str(result.grand_total) if result.grand_total is not None else None),
                _safe_cell(vf.get("currency", "VND")),
                "", "", "", "", "", "", "", "", "",
            ])

        for li in line_items:
            writer.writerow([
                _safe_cell(vf.get("invoice_number")),
                _safe_cell(vf.get("invoice_date")),
                _safe_cell(vf.get("seller_tax_code")),
                _safe_cell(vf.get("buyer_tax_code")),
                _safe_cell(vf.get("seller_name")),
                _safe_cell(vf.get("buyer_name")),
                _safe_cell(str(result.grand_total) if result.grand_total is not None else None),
                _safe_cell(vf.get("currency", "VND")),
                _safe_cell(str(li.line_number)),
                _safe_cell(li.item_name),
                _safe_cell(li.item_code),
                _safe_cell(li.unit),
                _safe_cell(str(li.quantity)),
                _safe_cell(str(li.unit_price)),
                _safe_cell(str(li.tax_rate)),
                _safe_cell(str(li.tax_amount)),
                _safe_cell(str(li.total_amount)),
            ])
        yield buf.getvalue()

    filename = f"{document_id}.csv"
    return StreamingResponse(
        _gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{document_id}/export/json", summary="Export extraction result as structured JSON")
async def export_json(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    doc, result = await _load_result(document_id, tenant, session)
    vf = result.validated_fields
    line_items = sorted(result.line_items, key=lambda x: x.line_number)
    field_conf_map = {fc.field_name: fc.confidence for fc in result.field_confidences}

    return {
        "schema_version": "1.0",
        "document_id": str(document_id),
        "doc_type": doc.doc_type,
        "extracted_at": result.created_at.isoformat(),
        "header": {
            "invoice_number": vf.get("invoice_number"),
            "invoice_date": vf.get("invoice_date"),
            "seller_tax_id": vf.get("seller_tax_code"),
            "seller_name": vf.get("seller_name"),
            "buyer_tax_id": vf.get("buyer_tax_code"),
            "buyer_name": vf.get("buyer_name"),
        },
        "totals": {
            "subtotal": str(result.subtotal) if result.subtotal is not None else None,
            "total_tax": str(result.total_tax) if result.total_tax is not None else None,
            "grand_total": str(result.grand_total) if result.grand_total is not None else None,
            "currency": vf.get("currency", "VND"),
        },
        "line_items": [
            {
                "line_number": li.line_number,
                "item_name": li.item_name,
                "item_code": li.item_code,
                "unit": li.unit,
                "quantity": str(li.quantity),
                "unit_price": str(li.unit_price),
                "tax_rate": str(li.tax_rate),
                "tax_amount": str(li.tax_amount),
                "total_amount": str(li.total_amount),
            }
            for li in line_items
        ],
        "confidence": {
            "document_score": result.confidence_score,
            "fields": field_conf_map,
        },
    }
