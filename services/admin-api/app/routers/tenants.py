"""Tenant CRUD and API-key management endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.models.audit import AuditLog
from einv_common.models.tenant import ApiKey, Tenant
from einv_common.models.user import AdminUser
from einv_common.schemas.common import PaginatedResponse
from einv_common.schemas.tenant import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    TenantCreate,
    TenantOut,
    TenantUpdate,
)
from app.auth_utils import generate_api_key
from app.deps import get_current_user, get_session, require_super_admin, require_tenant_admin

router = APIRouter()


def _check_tenant_access(user: AdminUser, tenant_id: uuid.UUID) -> None:
    """Raise 403 if the user can't act on this tenant."""
    if user.role == "super_admin":
        return
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


# ---------------------------------------------------------------------------
# Tenant endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedResponse)
async def list_tenants(
    page: int = 1,
    limit: int = 20,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
):
    offset = (page - 1) * limit
    total = (await session.execute(select(func.count()).select_from(Tenant))).scalar_one()
    rows = (await session.execute(select(Tenant).offset(offset).limit(limit))).scalars().all()
    items = [TenantOut.model_validate(t) for t in rows]
    return PaginatedResponse(
        items=items, total=total, page=page, limit=limit,
        pages=max(1, (total + limit - 1) // limit),
    )


@router.post("", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    existing = (await session.execute(
        select(Tenant).where(Tenant.slug == body.slug)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken")

    tenant = Tenant(**body.model_dump())
    session.add(tenant)
    session.add(AuditLog(
        tenant_id=tenant.id,
        action="tenant_created",
        actor=user.email,
        details={"slug": body.slug},
    ))
    await session.commit()
    await session.refresh(tenant)
    return TenantOut.model_validate(tenant)


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: uuid.UUID,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    _check_tenant_access(user, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantUpdate,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    _check_tenant_access(user, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(tenant, field, value)

    session.add(AuditLog(
        tenant_id=tenant_id,
        action="tenant_updated",
        actor=user.email,
        details=body.model_dump(exclude_none=True),
    ))
    await session.commit()
    await session.refresh(tenant)
    return TenantOut.model_validate(tenant)


@router.delete("/{tenant_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_tenant(
    tenant_id: uuid.UUID,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    tenant.is_active = False
    session.add(AuditLog(
        tenant_id=tenant_id,
        action="tenant_deactivated",
        actor=user.email,
        details={},
    ))
    await session.commit()


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------

@router.get("/{tenant_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    tenant_id: uuid.UUID,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyOut]:
    _check_tenant_access(user, tenant_id)
    rows = (await session.execute(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id)
    )).scalars().all()
    return [ApiKeyOut.model_validate(k) for k in rows]


@router.post("/{tenant_id}/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    tenant_id: uuid.UUID,
    body: ApiKeyCreate,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreated:
    _check_tenant_access(user, tenant_id)
    if await session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    raw_key, key_hash = generate_api_key()
    api_key = ApiKey(
        tenant_id=tenant_id,
        key_hash=key_hash,
        label=body.label,
        expires_at=body.expires_at,
    )
    session.add(api_key)
    session.add(AuditLog(
        tenant_id=tenant_id,
        action="api_key_created",
        actor=user.email,
        details={"label": body.label},
    ))
    await session.commit()
    await session.refresh(api_key)

    base = ApiKeyOut.model_validate(api_key)
    return ApiKeyCreated(raw_key=raw_key, **base.model_dump())


@router.delete(
    "/{tenant_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_api_key(
    tenant_id: uuid.UUID,
    key_id: uuid.UUID,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    _check_tenant_access(user, tenant_id)
    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    api_key.is_active = False
    session.add(AuditLog(
        tenant_id=tenant_id,
        action="key_rotated",
        actor=user.email,
        details={"key_id": str(key_id), "label": api_key.label},
    ))
    await session.commit()
