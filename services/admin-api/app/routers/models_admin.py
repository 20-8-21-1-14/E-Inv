"""Model version listing and activation endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.models.training import ModelVersion
from einv_common.models.user import AdminUser
from einv_common.schemas.common import PaginatedResponse
from einv_common.schemas.training import ModelVersionOut
from app.deps import get_session, require_super_admin

router = APIRouter()

_VALID_TYPES = {"det", "rec", "table", "layout", "sr"}


@router.get("", response_model=PaginatedResponse)
async def list_model_versions(
    page: int = 1,
    limit: int = 20,
    model_type: str | None = None,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
):
    query = select(ModelVersion)
    count_query = select(func.count()).select_from(ModelVersion)

    if model_type:
        if model_type not in _VALID_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"model_type must be one of {sorted(_VALID_TYPES)}",
            )
        query = query.where(ModelVersion.model_type == model_type)
        count_query = count_query.where(ModelVersion.model_type == model_type)

    total = (await session.execute(count_query)).scalar_one()
    offset = (page - 1) * limit
    rows = (await session.execute(
        query.order_by(ModelVersion.created_at.desc()).offset(offset).limit(limit)
    )).scalars().all()

    items = [ModelVersionOut.model_validate(r) for r in rows]
    return PaginatedResponse(
        items=items, total=total, page=page, limit=limit,
        pages=max(1, (total + limit - 1) // limit),
    )


@router.patch("/{model_id}/activate", response_model=ModelVersionOut)
async def activate_model_version(
    model_id: uuid.UUID,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> ModelVersionOut:
    mv = await session.get(ModelVersion, model_id)
    if mv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model version not found")

    # Deactivate other versions of the same model type
    await session.execute(
        update(ModelVersion)
        .where(ModelVersion.model_type == mv.model_type)
        .where(ModelVersion.id != model_id)
        .values(is_active=False)
    )
    mv.is_active = True
    mv.promoted_at = datetime.now(timezone.utc)
    mv.promoted_by = user.id
    await session.commit()
    await session.refresh(mv)
    return ModelVersionOut.model_validate(mv)
