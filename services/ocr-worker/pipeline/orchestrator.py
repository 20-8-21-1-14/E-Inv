"""Pipeline orchestrator — main entry point for document processing.

Async pipeline stages:
  XML path  : detect → xml_parse → validate → score → persist → webhook
  Image path: detect → preprocess → ocr → extract → validate → score
               → [llm_fallback if needed] → persist → webhook

`run(document_id, tenant_id)` is the function called from tasks.py.
It loads document + tenant from DB, fetches content from MinIO, runs the
appropriate path, and writes the final ExtractionResult + line items.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from einv_common.db import session_factory
from einv_common.exceptions import LLMError, NonRetryableError, UnsupportedFormatError
from einv_common.models.document import Document
from einv_common.models.extraction import ExtractionResult, FieldConfidence, InvoiceLineItem
from einv_common.models.hitl import HitlQueue
from einv_common.models.tenant import Tenant
from einv_common.config import settings
from einv_common.storage import get_storage_client
from pipeline.detector import bytes_to_image, detect_format, merge_page_results, pdf_to_images
from pipeline.extractor import extract
from pipeline.models import ExtractionData, FieldData, PipelineResult, ValidationError
from pipeline.ocr_engine import run_ocr_pages
from pipeline.preprocessor import preprocess
from pipeline.scorer import compute_score
from pipeline.validator import validate as validate_extraction
from pipeline.webhook_dispatcher import fire_and_forget
from pipeline.xml_parser import parse_xml

logger = structlog.get_logger()

_CPU_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline_cpu")


# ---------------------------------------------------------------------------
# Entry point called by tasks.py
# ---------------------------------------------------------------------------

async def run(document_id: str, tenant_id: str) -> dict:
    """Fetch document → run pipeline → persist result.

    Returns a dict summary: {"status": ..., "confidence_score": ..., "ocr_engine": ...}
    Raises on unrecoverable error (Celery will dead-letter).
    """
    doc_uuid    = UUID(document_id)
    tenant_uuid = UUID(tenant_id)

    async with session_factory() as session:
        # ── Load document and tenant ─────────────────────────────────────────
        doc = await _load_document(session, doc_uuid, tenant_uuid)
        tenant = await _load_tenant(session, tenant_uuid)

        # ── Mark processing ──────────────────────────────────────────────────
        doc.status = "processing"
        await session.commit()

        try:
            # ── Fetch raw content from MinIO ─────────────────────────────────
            storage = get_storage_client()
            content = await storage.download(
                bucket=settings.minio_bucket_raw,
                key=doc.file_path,
            )

            # ── Run the pipeline ─────────────────────────────────────────────
            threshold_done = float(os.environ.get("OCR_THRESHOLD_DONE", str(tenant.confidence_threshold)))
            threshold_llm  = float(os.environ.get("OCR_THRESHOLD_LLM", "0.75"))
            anthropic_key  = os.environ.get("ANTHROPIC_API_KEY")

            result, unmatched_headers = await _run_pipeline(
                content=content,
                declared_format=doc.source_format,
                threshold_done=threshold_done,
                threshold_llm=threshold_llm,
                anthropic_api_key=anthropic_key,
            )

            # ── Persist result ───────────────────────────────────────────────
            final_status = await _persist_result(
                session=session,
                doc=doc,
                tenant=tenant,
                result=result,
                threshold_done=threshold_done,
            )

            # ── Record unmatched column headers for schema evolution ──────────
            if unmatched_headers:
                await _record_alias_proposals(session, unmatched_headers, doc.doc_type)
            doc.status = final_status
            doc.processed_at = datetime.now(timezone.utc)
            await session.commit()

            # ── Fire webhook ─────────────────────────────────────────────────
            if tenant.webhook_url:
                fire_and_forget(
                    webhook_url=tenant.webhook_url,
                    document_id=document_id,
                    tenant_id=tenant_id,
                    status=final_status,
                    confidence_score=result.confidence_score,
                    ocr_engine=result.ocr_engine,
                )

            return {
                "status": final_status,
                "confidence_score": result.confidence_score,
                "ocr_engine": result.ocr_engine,
            }

        except Exception as exc:
            await session.rollback()
            doc.status = "failed"
            doc.error_message = str(exc)[:1000]
            doc.processed_at = datetime.now(timezone.utc)
            await session.commit()

            if tenant.webhook_url:
                fire_and_forget(
                    webhook_url=tenant.webhook_url,
                    document_id=document_id,
                    tenant_id=tenant_id,
                    status="failed",
                    confidence_score=None,
                    ocr_engine=None,
                )
            raise


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

async def _run_pipeline(
    *,
    content: bytes,
    declared_format: str,
    threshold_done: float,
    threshold_llm: float,
    anthropic_api_key: str | None,
) -> tuple[PipelineResult, list[str]]:
    """Returns (PipelineResult, unmatched_column_headers)."""
    t0 = time.monotonic()
    preprocess_steps: list[str] = []
    all_unmatched: list[str] = []

    # ── Stage 1: Format detection ─────────────────────────────────────────────
    fmt = await detect_format(content, declared_format)
    logger.info("pipeline.format_detected", fmt=fmt)

    # ── Stage 2: XML path (fast, 100% accuracy) ───────────────────────────────
    if fmt == "xml":
        extraction = parse_xml(content)
        validation_errors = validate_extraction(extraction)
        # Assign confidence 1.0 for every populated field — XML is the canonical source
        field_confs = _xml_synthetic_confs(extraction)
        score, routing = compute_score(
            extraction, field_confs, validation_errors,
            threshold_done=threshold_done, threshold_llm=threshold_llm,
        )
        ocr_engine = "xml_bypass"
        # XML path has no column mapping — nothing to collect

    # ── Stage 3: Image / PDF path ─────────────────────────────────────────────
    elif fmt in ("pdf", "image"):
        if fmt == "pdf":
            pages = await pdf_to_images(content)
        else:
            img = await bytes_to_image(content)
            pages = [img]

        # Preprocess pages concurrently in CPU executor
        loop = asyncio.get_running_loop()
        preprocess_results = await asyncio.gather(*[
            loop.run_in_executor(_CPU_EXECUTOR, preprocess, img)
            for img in pages
        ])
        preprocess_steps = list(preprocess_results[0].steps_applied)
        preprocessed_images = [r.image for r in preprocess_results]

        # OCR all pages
        ocr_results = await run_ocr_pages(preprocessed_images)

        # Extract per page then merge
        page_extractions: list[ExtractionData] = []
        all_field_confs: list[FieldData] = []
        all_unmatched: list[str] = []
        for ocr_result in ocr_results:
            page_data, page_confs, page_unmatched = extract(ocr_result)
            page_extractions.append(page_data)
            all_field_confs.extend(page_confs)
            all_unmatched.extend(page_unmatched)

        extraction = merge_page_results(page_extractions)
        field_confs = all_field_confs
        validation_errors = validate_extraction(extraction)
        score, routing = compute_score(
            extraction, field_confs, validation_errors,
            threshold_done=threshold_done, threshold_llm=threshold_llm,
        )
        ocr_engine = "paddleocr"

        # ── LLM fallback ────────────────────────────────────────────────────
        if routing == "llm":
            if not anthropic_api_key:
                logger.warning("pipeline.llm_skipped_no_key")
                routing = "hitl"
            else:
                try:
                    from pipeline.llm_fallback import run_llm_fallback
                    extraction, field_confs = await run_llm_fallback(
                        preprocessed_images[0], extraction, field_confs, anthropic_api_key
                    )
                    validation_errors = validate_extraction(extraction)
                    score, routing = compute_score(
                        extraction, field_confs, validation_errors,
                        threshold_done=threshold_done, threshold_llm=threshold_llm,
                    )
                    ocr_engine = "llm_fallback"
                except LLMError as exc:
                    logger.warning("pipeline.llm_error", error=str(exc))
                    routing = "hitl"

    else:
        raise UnsupportedFormatError(f"Unsupported document format: {fmt}")

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    pipeline_result = PipelineResult(
        extraction=extraction,
        field_confidences=field_confs,
        validation_errors=validation_errors,
        confidence_score=score,
        ocr_engine=ocr_engine,
        processing_time_ms=elapsed_ms,
        preprocess_steps=preprocess_steps,
    )
    return pipeline_result, all_unmatched


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _load_document(session: AsyncSession, document_id: UUID, tenant_id: UUID) -> Document:
    result = await session.execute(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise NonRetryableError(f"Document {document_id} not found for tenant {tenant_id}")
    return doc


async def _load_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise NonRetryableError(f"Tenant {tenant_id} not found")
    return tenant


async def _persist_result(
    session: AsyncSession,
    doc: Document,
    tenant: Tenant,
    result: PipelineResult,
    threshold_done: float,
) -> str:
    """Write ExtractionResult + line items + field confidences. Returns final status string."""
    ext = result.extraction

    # Build raw_fields and validated_fields JSONB dicts from header data
    all_header = {
        "invoice_number":  ext.invoice_number,
        "invoice_date":    ext.invoice_date,
        "invoice_form":    ext.invoice_form,
        "invoice_series":  ext.invoice_series,
        "seller_name":     ext.seller_name,
        "seller_tax_code": ext.seller_tax_code,
        "seller_address":  ext.seller_address,
        "seller_bank":     ext.seller_bank,
        "buyer_name":      ext.buyer_name,
        "buyer_tax_code":  ext.buyer_tax_code,
        "buyer_address":   ext.buyer_address,
        "payment_method":  ext.payment_method,
        "currency":        ext.currency or "VND",
        "amount_in_words": ext.amount_in_words,
        "total_discount":  str(ext.total_discount) if ext.total_discount is not None else None,
    }
    raw_fields = {k: v for k, v in all_header.items() if v is not None}

    extraction_record = ExtractionResult(
        document_id=doc.id,
        tenant_id=doc.tenant_id,
        ocr_engine=result.ocr_engine,
        confidence_score=result.confidence_score,
        processing_time_ms=result.processing_time_ms,
        subtotal=ext.subtotal,
        total_tax=ext.total_tax,
        grand_total=ext.grand_total,
        raw_fields=raw_fields,
        validated_fields=dict(raw_fields),  # copy; HITL corrections update validated_fields later
        validation_errors=[
            {"field": e.field, "message": e.message, "severity": e.severity}
            for e in result.validation_errors
        ],
    )
    session.add(extraction_record)
    await session.flush()   # Populate extraction_record.id before FK references

    # Line items
    for item in ext.line_items:
        session.add(InvoiceLineItem(
            result_id=extraction_record.id,
            document_id=doc.id,
            tenant_id=doc.tenant_id,
            line_number=item.line_number,
            item_name=item.item_name,
            item_code=item.item_code,
            unit=item.unit,
            quantity=item.quantity,
            unit_price=item.unit_price,
            amount=item.amount,
            discount_rate=item.discount_rate,
            discount_amount=item.discount_amount,
            tax_rate=item.tax_rate,
            tax_amount=item.tax_amount,
            total_amount=item.total_amount,
            name_confidence=item.name_confidence,
            qty_confidence=item.qty_confidence,
            unit_confidence=item.unit_confidence,
            price_confidence=item.price_confidence,
        ))

    # Field confidences (non-line-item fields)
    for fd in result.field_confidences:
        if "." not in fd.name:  # Skip per-line-item confidences (stored on InvoiceLineItem)
            session.add(FieldConfidence(
                result_id=extraction_record.id,
                tenant_id=doc.tenant_id,
                field_name=fd.name,
                raw_value=fd.raw_value,
                confidence=fd.confidence,
                exported=False,
                bbox=(
                    {"x1": fd.bbox[0], "y1": fd.bbox[1], "x2": fd.bbox[2], "y2": fd.bbox[3]}
                    if fd.bbox else None
                ),
            ))

    # Determine final status
    final_status = "done" if result.confidence_score >= threshold_done else "hitl"

    # HITL queue entry
    if final_status == "hitl":
        low_conf_summary = ", ".join(
            f"{f.name}={f.confidence:.2f}" for f in result.low_confidence_fields[:10]
        )
        session.add(HitlQueue(
            document_id=doc.id,
            tenant_id=doc.tenant_id,
            reason="low_confidence",
            notes=f"score={result.confidence_score:.3f}; low_conf_fields: {low_conf_summary}",
        ))

    return final_status


def _xml_synthetic_confs(extraction: ExtractionData) -> list[FieldData]:
    """Generate 1.0-confidence FieldData entries for all populated XML fields.

    The scorer's weighted average starts from 0.5 when field_confs is empty.
    XML data is canonical (from the issuer's signed file), so every present
    field should score 1.0, not 0.5.
    """
    confs: list[FieldData] = []
    for attr in (
        "invoice_number", "invoice_date", "invoice_form", "invoice_series",
        "seller_name", "seller_tax_code", "seller_address",
        "buyer_name", "buyer_tax_code",
        "grand_total", "total_tax", "subtotal",
    ):
        val = getattr(extraction, attr, None)
        if val is not None:
            confs.append(FieldData(name=attr, value=str(val), confidence=1.0))

    for idx, item in enumerate(extraction.line_items, start=1):
        for field_name in ("item_name", "quantity", "unit_price", "amount"):
            val = getattr(item, field_name, None)
            if val is not None:
                confs.append(FieldData(
                    name=f"line_{idx}.{field_name}",
                    value=str(val),
                    confidence=1.0,
                ))
    return confs


async def _record_alias_proposals(
    session: AsyncSession,
    unmatched_headers: list[str],
    doc_type: str,
) -> None:
    """Upsert ColumnAliasProposal records for headers that failed to map.

    Uses INSERT … ON CONFLICT DO UPDATE so repeated occurrences just increment
    seen_count and update last_seen_at — no duplicate rows.
    """
    from datetime import datetime, timezone
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from einv_common.models.training import ColumnAliasProposal

    now = datetime.now(timezone.utc)
    for header in set(unmatched_headers):  # deduplicate within one document
        stmt = pg_insert(ColumnAliasProposal).values(
            unmatched_header=header,
            doc_type=doc_type,
            seen_count=1,
            first_seen_at=now,
            last_seen_at=now,
        ).on_conflict_do_update(
            constraint="uq_proposal_header_doctype",
            set_={
                "seen_count": ColumnAliasProposal.seen_count + 1,
                "last_seen_at": now,
            },
        )
        await session.execute(stmt)

    logger.info("orchestrator.alias_proposals_recorded", count=len(set(unmatched_headers)))
