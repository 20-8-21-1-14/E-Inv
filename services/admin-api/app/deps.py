"""Shared FastAPI dependency injectors for admin-api."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.db import session_factory
from einv_common.models.user import AdminUser
from app.auth_utils import decode_token

_bearer = HTTPBearer(auto_error=False)


async def get_session():
    async with session_factory() as session:
        yield session


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> AdminUser:
    _unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise _unauthorized
    try:
        payload = decode_token(creds.credentials)
    except InvalidTokenError:
        raise _unauthorized

    if payload.get("type") != "access":
        raise _unauthorized

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise _unauthorized

    user = await session.get(AdminUser, user_id)
    if user is None or not user.is_active:
        raise _unauthorized
    return user


def require_super_admin(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    if user.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="super_admin role required",
        )
    return user


def require_tenant_admin(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    if user.role not in ("super_admin", "tenant_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant_admin role required",
        )
    return user


def require_reviewer(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    """Any authenticated admin role may access HITL review endpoints."""
    return user
