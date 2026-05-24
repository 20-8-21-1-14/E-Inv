"""Schema version management and column alias proposal review."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.models.training import ColumnAliasProposal, SchemaVersion
from einv_common.models.user import AdminUser
from einv_common.schemas.common import PaginatedResponse
from einv_common.schemas.training import (
    ColumnAliasOut,
    ColumnAliasReview,
    SchemaVersionCreate,
    SchemaVersionOut,
)
from app.deps import get_session, require_super_admin, require_tenant_admin

router = APIRouter()


# ---------------------------------------------------------------------------
# Schema versions
# ---------------------------------------------------------------------------

@router.get("/schemas", response_model=list[SchemaVersionOut])
async def list_schema_versions(
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> list[SchemaVersionOut]:
    rows = (await session.execute(
        select(SchemaVersion).order_by(SchemaVersion.created_at.desc())
    )).scalars().all()
    return [SchemaVersionOut.model_validate(r) for r in rows]


@router.post("/schemas", response_model=SchemaVersionOut, status_code=status.HTTP_201_CREATED)
async def create_schema_version(
    body: SchemaVersionCreate,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> SchemaVersionOut:
    existing = (await session.execute(
        select(SchemaVersion).where(SchemaVersion.version == body.version)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Schema version {body.version!r} already exists",
        )
    sv = SchemaVersion(
        version=body.version,
        content=body.content,
        changelog=body.changelog,
        is_active=False,
        created_by=user.id,
    )
    session.add(sv)
    await session.commit()
    await session.refresh(sv)
    return SchemaVersionOut.model_validate(sv)


@router.post("/schemas/{schema_id}/activate", response_model=SchemaVersionOut)
async def activate_schema_version(
    schema_id: uuid.UUID,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> SchemaVersionOut:
    sv = await session.get(SchemaVersion, schema_id)
    if sv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema version not found")

    # Deactivate all other versions atomically
    await session.execute(
        update(SchemaVersion)
        .where(SchemaVersion.id != schema_id)
        .values(is_active=False)
    )
    sv.is_active = True
    sv.activated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(sv)
    return SchemaVersionOut.model_validate(sv)


# ---------------------------------------------------------------------------
# Column alias proposals
# ---------------------------------------------------------------------------

@router.get("/column-aliases", response_model=PaginatedResponse)
async def list_column_aliases(
    page: int = 1,
    limit: int = 20,
    status_filter: str | None = None,
    _: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
):
    query = select(ColumnAliasProposal)
    count_query = select(func.count()).select_from(ColumnAliasProposal)

    if status_filter:
        query = query.where(ColumnAliasProposal.status == status_filter)
        count_query = count_query.where(ColumnAliasProposal.status == status_filter)

    total = (await session.execute(count_query)).scalar_one()
    offset = (page - 1) * limit
    rows = (await session.execute(
        query.order_by(ColumnAliasProposal.seen_count.desc()).offset(offset).limit(limit)
    )).scalars().all()

    items = [ColumnAliasOut.model_validate(r) for r in rows]
    return PaginatedResponse(
        items=items, total=total, page=page, limit=limit,
        pages=max(1, (total + limit - 1) // limit),
    )


@router.patch("/column-aliases/{alias_id}", response_model=ColumnAliasOut)
async def review_column_alias(
    alias_id: uuid.UUID,
    body: ColumnAliasReview,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> ColumnAliasOut:
    proposal = await session.get(ColumnAliasProposal, alias_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    if proposal.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Proposal already {proposal.status}",
        )

    proposal.status = body.status
    proposal.suggested_field = body.suggested_field
    proposal.notes = body.notes
    proposal.reviewed_by = user.id
    proposal.reviewed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(proposal)
    return ColumnAliasOut.model_validate(proposal)
