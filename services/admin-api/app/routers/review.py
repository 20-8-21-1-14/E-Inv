"""HITL review queue and field correction endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.models.audit import AuditLog
from einv_common.models.extraction import ExtractionResult, FieldConfidence, InvoiceLineItem
from einv_common.models.hitl import HitlQueue
from einv_common.models.user import AdminUser
from einv_common.schemas.common import PaginatedResponse
from einv_common.schemas.extraction import ExtractionResultOut
from einv_common.schemas.hitl import HitlCorrectionIn, HitlItemOut
from app.deps import get_session, require_reviewer

router = APIRouter()


class AssignIn(BaseModel):
    assigned_to: uuid.UUID | None = None


class ReviewDetailOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    tenant_id: uuid.UUID
    reason: str
    status: str
    assigned_to: str | None
    notes: str | None
    created_at: datetime
    resolved_at: datetime | None
    extraction: ExtractionResultOut | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_hitl_or_404(session: AsyncSession, hitl_id: uuid.UUID, user: AdminUser) -> HitlQueue:
    item = await session.get(HitlQueue, hitl_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review item not found")
    if user.role != "super_admin" and item.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return item


# ---------------------------------------------------------------------------
# Review queue list
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedResponse)
async def list_review_queue(
    page: int = 1,
    limit: int = 20,
    status_filter: str | None = None,
    user: AdminUser = Depends(require_reviewer),
    session: AsyncSession = Depends(get_session),
):
    query = select(HitlQueue)
    count_query = select(func.count()).select_from(HitlQueue)

    if user.role != "super_admin":
        query = query.where(HitlQueue.tenant_id == user.tenant_id)
        count_query = count_query.where(HitlQueue.tenant_id == user.tenant_id)

    if status_filter:
        query = query.where(HitlQueue.status == status_filter)
        count_query = count_query.where(HitlQueue.status == status_filter)

    total = (await session.execute(count_query)).scalar_one()
    offset = (page - 1) * limit
    rows = (await session.execute(
        query.order_by(HitlQueue.created_at.desc()).offset(offset).limit(limit)
    )).scalars().all()

    items = [HitlItemOut.model_validate(r) for r in rows]
    return PaginatedResponse(
        items=items, total=total, page=page, limit=limit,
        pages=max(1, (total + limit - 1) // limit),
    )


@router.get("/{hitl_id}", response_model=ReviewDetailOut)
async def get_review_item(
    hitl_id: uuid.UUID,
    user: AdminUser = Depends(require_reviewer),
    session: AsyncSession = Depends(get_session),
) -> ReviewDetailOut:
    item = await _get_hitl_or_404(session, hitl_id, user)

    extraction_row = (await session.execute(
        select(ExtractionResult).where(ExtractionResult.document_id == item.document_id)
    )).scalar_one_or_none()

    extraction_out: ExtractionResultOut | None = None
    if extraction_row is not None:
        # Load relationships while session is open
        await session.refresh(extraction_row, ["line_items", "field_confidences"])
        extraction_out = ExtractionResultOut.model_validate(extraction_row)

    return ReviewDetailOut(
        id=item.id,
        document_id=item.document_id,
        tenant_id=item.tenant_id,
        reason=item.reason,
        status=item.status,
        assigned_to=str(item.assigned_to) if item.assigned_to else None,
        notes=item.notes,
        created_at=item.created_at,
        resolved_at=item.resolved_at,
        extraction=extraction_out,
    )


# ---------------------------------------------------------------------------
# Assign
# ---------------------------------------------------------------------------

@router.patch("/{hitl_id}/assign", response_model=HitlItemOut)
async def assign_review_item(
    hitl_id: uuid.UUID,
    body: AssignIn,
    user: AdminUser = Depends(require_reviewer),
    session: AsyncSession = Depends(get_session),
) -> HitlItemOut:
    item = await _get_hitl_or_404(session, hitl_id, user)
    item.assigned_to = body.assigned_to
    if item.status == "pending":
        item.status = "in_review"
    await session.commit()
    await session.refresh(item)
    return HitlItemOut.model_validate(item)


# ---------------------------------------------------------------------------
# Bulk corrections (field + line-item) applied against a review item
# ---------------------------------------------------------------------------

@router.patch("/{hitl_id}/corrections", response_model=HitlItemOut)
async def apply_corrections(
    hitl_id: uuid.UUID,
    body: HitlCorrectionIn,
    user: AdminUser = Depends(require_reviewer),
    session: AsyncSession = Depends(get_session),
) -> HitlItemOut:
    item = await _get_hitl_or_404(session, hitl_id, user)

    extraction = (await session.execute(
        select(ExtractionResult).where(ExtractionResult.document_id == item.document_id)
    )).scalar_one_or_none()
    if extraction is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="No extraction result for this document")

    now = datetime.now(timezone.utc)

    # Field-level corrections (non-line-item fields → FieldConfidence table)
    for fc_in in body.field_corrections:
        fc_row = (await session.execute(
            select(FieldConfidence)
            .where(FieldConfidence.result_id == extraction.id)
            .where(FieldConfidence.field_name == fc_in.field_name)
        )).scalar_one_or_none()
        if fc_row is None:
            continue
        old_value = fc_row.raw_value
        fc_row.corrected_value = fc_in.corrected_value
        fc_row.is_corrected = True
        fc_row.corrected_by = user.email
        fc_row.corrected_at = now
        session.add(AuditLog(
            tenant_id=item.tenant_id,
            document_id=item.document_id,
            action="corrected",
            actor=user.email,
            details={
                "field_name": fc_in.field_name,
                "old_value": old_value,
                "new_value": fc_in.corrected_value,
            },
        ))

    # Line-item corrections (InvoiceLineItem table)
    for li_in in body.line_item_corrections:
        li_row = await session.get(InvoiceLineItem, li_in.line_item_id)
        if li_row is None or li_row.result_id != extraction.id:
            continue
        changed: dict = {}
        for field in ("item_name", "item_code", "unit", "quantity", "unit_price", "tax_rate"):
            val = getattr(li_in, field)
            if val is not None:
                changed[field] = {"old": str(getattr(li_row, field)), "new": str(val)}
                setattr(li_row, field, val)
        li_row.is_corrected = True
        li_row.corrected_by = user.email
        li_row.corrected_at = now
        if changed:
            session.add(AuditLog(
                tenant_id=item.tenant_id,
                document_id=item.document_id,
                action="corrected",
                actor=user.email,
                details={"line_item_id": str(li_in.line_item_id), "changes": changed},
            ))

    item.status = "in_review"
    await session.commit()
    await session.refresh(item)
    return HitlItemOut.model_validate(item)


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

@router.patch("/{hitl_id}/resolve", response_model=HitlItemOut)
async def resolve_review_item(
    hitl_id: uuid.UUID,
    user: AdminUser = Depends(require_reviewer),
    session: AsyncSession = Depends(get_session),
) -> HitlItemOut:
    item = await _get_hitl_or_404(session, hitl_id, user)
    item.status = "resolved"
    item.resolved_at = datetime.now(timezone.utc)
    session.add(AuditLog(
        tenant_id=item.tenant_id,
        document_id=item.document_id,
        action="hitl_resolved",
        actor=user.email,
        details={"hitl_id": str(hitl_id)},
    ))
    await session.commit()
    await session.refresh(item)
    return HitlItemOut.model_validate(item)

