"""Shared FastAPI dependencies for core-api."""

import hashlib
import time
from datetime import datetime, timezone
from functools import lru_cache

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from einv_common.config import settings
from einv_common.db import get_session
from einv_common.models import ApiKey, Tenant
from einv_common.storage import StorageClient, get_storage_client

_security = HTTPBearer(auto_error=False)

# ── Redis ─────────────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


# ── Storage ───────────────────────────────────────────────────────────────────

async def get_storage() -> StorageClient:
    return get_storage_client()


# ── Rate limiting ─────────────────────────────────────────────────────────────

_RATE_LIMIT = 120        # requests
_RATE_WINDOW = 60        # seconds


async def _enforce_rate_limit(tenant_id: str, redis: aioredis.Redis) -> None:
    window = int(time.time()) // _RATE_WINDOW
    key = f"rate:{tenant_id}:{window}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _RATE_WINDOW * 2)
    if count > _RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail={"code": "RATE_LIMIT_EXCEEDED", "message": "Too many requests. Retry after 60 seconds."},
            headers={"Retry-After": str(_RATE_WINDOW)},
        )


# ── Auth ──────────────────────────────────────────────────────────────────────

async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    session: AsyncSession = Depends(get_session),
    redis: aioredis.Redis = Depends(get_redis),
) -> Tenant:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"code": "MISSING_AUTH", "message": "Authorization: Bearer <api-key> required"},
        )

    raw_key = credentials.credentials
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    result = await session.execute(
        select(ApiKey)
        .where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
        .options(selectinload(ApiKey.tenant))
    )
    api_key = result.scalar_one_or_none()

    if api_key is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_API_KEY", "message": "API key not found or inactive"},
        )

    now = datetime.now(timezone.utc)
    if api_key.expires_at and api_key.expires_at < now:
        raise HTTPException(
            status_code=401,
            detail={"code": "API_KEY_EXPIRED", "message": "API key has expired"},
        )

    tenant: Tenant = api_key.tenant
    if not tenant.is_active:
        raise HTTPException(
            status_code=403,
            detail={"code": "TENANT_INACTIVE", "message": "Tenant account is disabled"},
        )

    # Rate limit before processing the request
    await _enforce_rate_limit(str(tenant.id), redis)

    # Update last_used_at — included in the auto-commit at end of session
    await session.execute(
        update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now)
    )

    return tenant
