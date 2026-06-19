"""Per-tenant upload quota enforcement via atomic Redis Lua sliding window."""

import uuid
import structlog
import redis.asyncio as aioredis
from fastapi import HTTPException

from einv_common.models.tenant import Tenant

logger = structlog.get_logger()

# Atomic sliding-window Lua script.
# KEYS[1] = "ratelimit:{tenant_id}"
# ARGV[1] = quota_max_docs  (int)
# ARGV[2] = quota_window_ms (int, milliseconds)
# ARGV[3] = ratelimit_event_id (unique string per upload attempt)
#
# Returns: {remaining, retry_after_seconds}
#   remaining == -1  →  quota exceeded; retry_after_seconds > 0
#   remaining >= 0   →  accepted; retry_after_seconds == 0
_LUA_SCRIPT = """
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window_start = now_ms - tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, window_start)
local count = redis.call('ZCARD', KEYS[1])

if count >= tonumber(ARGV[1]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_after = math.ceil((tonumber(oldest[2]) + tonumber(ARGV[2]) - now_ms) / 1000)
  return {-1, math.max(1, retry_after)}
end

redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return {tonumber(ARGV[1]) - count - 1, 0}
"""


async def check_quota(tenant: Tenant, redis: aioredis.Redis) -> None:
    """Enforce per-tenant upload quota. Raises 429 if exceeded. Fail-open on Redis errors."""
    if tenant.quota_max_docs is None or tenant.quota_window_seconds is None:
        return  # unlimited tenant

    if tenant.quota_max_docs <= 0 or tenant.quota_window_seconds <= 0:
        logger.warning(
            "ratelimit.invalid_quota_config",
            tenant_id=str(tenant.id),
            quota_max_docs=tenant.quota_max_docs,
            quota_window_seconds=tenant.quota_window_seconds,
        )
        return  # misconfigured — fail-open rather than blocking all uploads

    key = f"ratelimit:{tenant.id}"
    window_ms = tenant.quota_window_seconds * 1000
    event_id = str(uuid.uuid4())

    try:
        result = await redis.eval(
            _LUA_SCRIPT,
            1,          # number of KEYS
            key,        # KEYS[1]
            str(tenant.quota_max_docs),
            str(window_ms),
            event_id,
        )
        remaining, retry_after = int(result[0]), int(result[1])
    except Exception as exc:
        logger.warning(
            "ratelimit.redis_unavailable",
            tenant_id=str(tenant.id),
            error=str(exc),
        )
        return  # fail-open

    if remaining == -1:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "QUOTA_EXCEEDED",
                "message": (
                    f"Upload quota of {tenant.quota_max_docs} documents per "
                    f"{tenant.quota_window_seconds}s exceeded."
                ),
            },
            headers={"Retry-After": str(retry_after)},
        )
