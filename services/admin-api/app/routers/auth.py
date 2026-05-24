"""JWT authentication endpoints — login, refresh, me."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.models.user import AdminUser
from app.auth_utils import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.deps import get_current_user, get_session

router = APIRouter()


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    tenant_id: uuid.UUID | None
    is_active: bool


@router.post("/login", response_model=TokenOut)
async def login(
    body: LoginIn,
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
    result = await session.execute(
        select(AdminUser).where(AdminUser.email == body.email)
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    access = create_access_token(
        str(user.id), user.email, user.role,
        str(user.tenant_id) if user.tenant_id else None,
    )
    refresh = create_refresh_token(str(user.id))
    return TokenOut(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    body: RefreshIn,
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
    try:
        payload = decode_token(body.refresh_token)
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token")

    user = await session.get(AdminUser, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    access = create_access_token(
        str(user.id), user.email, user.role,
        str(user.tenant_id) if user.tenant_id else None,
    )
    new_refresh = create_refresh_token(str(user.id))
    return TokenOut(access_token=access, refresh_token=new_refresh)


@router.get("/me", response_model=UserOut)
async def me(user: AdminUser = Depends(get_current_user)) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        is_active=user.is_active,
    )
