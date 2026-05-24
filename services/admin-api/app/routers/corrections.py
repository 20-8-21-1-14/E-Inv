"""Direct field-level correction endpoint — PATCH /admin/field-corrections/{fc_id}."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.models.audit import AuditLog
from einv_common.models.extraction import FieldConfidence
from einv_common.models.user import AdminUser
from einv_common.schemas.extraction import FieldConfidenceOut
from app.deps import get_session, require_reviewer

router = APIRouter()


class FieldCorrectionPatch(BaseModel):
    corrected_value: str


@router.patch("/{fc_id}", response_model=FieldConfidenceOut)
async def correct_field(
    fc_id: uuid.UUID,
    body: FieldCorrectionPatch,
    user: AdminUser = Depends(require_reviewer),
    session: AsyncSession = Depends(get_session),
) -> FieldConfidenceOut:
    fc = await session.get(FieldConfidence, fc_id)
    if fc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Field record not found")
    if user.role != "super_admin" and fc.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    old_value = fc.raw_value
    fc.corrected_value = body.corrected_value
    fc.is_corrected = True
    fc.corrected_by = user.email
    fc.corrected_at = datetime.now(timezone.utc)

    session.add(AuditLog(
        tenant_id=fc.tenant_id,
        document_id=None,
        action="corrected",
        actor=user.email,
        details={
            "fc_id": str(fc_id),
            "field_name": fc.field_name,
            "old_value": old_value,
            "new_value": body.corrected_value,
        },
    ))
    await session.commit()
    await session.refresh(fc)
    return FieldConfidenceOut.model_validate(fc)
