from typing import Any
from uuid import UUID

import redis.asyncio as redis

from server.utils.constants import CAPPED_TIME_WINDOW, DOWNLOAD_CAP_BYTES

# adapted from - https://github.com/alisaifee/limits,
# https://redis.io/tutorials/howtos/ratelimiting/#1-fixed-window-counter

# Keys:   KEYS[1] = user_sub       (user hash key)
# Args:   ARGV[1] = request_range  (bytes being requested)
#         ARGV[2] = cap            (total bytes allowed per window)
#         ARGV[3] = window         (window duration in seconds)
#         ARGV[4] = now            (current unix timestamp as float)
#
# Returns:
#   >= 0  → allowed; value is remaining bytes after deduction
#     -1  → denied (cap exceeded, or single request > cap on a fresh window)
_DEDUCT_SCRIPT = """
local key         = KEYS[1]
local amount      = tonumber(ARGV[1])
local cap         = tonumber(ARGV[2])
local window      = tonumber(ARGV[3])
local now         = tonumber(ARGV[4])

local remaining   = tonumber(redis.call('HGET', key, 'remaining_bytes'))
local expire_time = tonumber(redis.call('HGET', key, 'expire_time'))

if remaining ~= nil and expire_time ~= nil and (expire_time - now) > 0 then
    if (remaining - amount) >= 0 then
        local new_remaining = remaining - amount
        redis.call('HSET', key, 'remaining_bytes', new_remaining)
        return new_remaining
    else
        return -1
    end
end

-- No active window (new user or expired). Start a fresh window.
if amount > cap then
    return -1
end
local new_remaining = cap - amount
local new_expire    = now + window
redis.call('HSET', key, 'remaining_bytes', new_remaining, 'expire_time', new_expire)
redis.call('EXPIREAT', key, math.ceil(new_expire))
return new_remaining
"""

# Refunds bytes to download budget after a failed GCS request,
# if the time window is still active.
# Keys:   KEYS[1] = user_sub (user hash key)
# Args:   ARGV[1] = amount   (bytes to refund)
#         ARGV[2] = now      (current unix timestamp)
_REFUND_SCRIPT = """
local key         = KEYS[1]
local amount      = tonumber(ARGV[1])
local now         = tonumber(ARGV[2])

local expire_time = tonumber(redis.call('HGET', key, 'expire_time'))

if expire_time ~= nil and (expire_time - now) > 0 then
    redis.call('HINCRBY', key, 'remaining_bytes', amount)
end
return 1
"""

# Release the lock safely
# Refer - https://redis.io/docs/latest/commands/set/ for this lock pattern
# Keys:   KEYS[1] = hashed token (user token hash)
# Args:   ARGV[1] = uuid         (request uuid)

_RELEASE_LOCK_SCRIPT = """
local key         = KEYS[1]
local uuid        = ARGV[1]

if redis.call('GET',key) == uuid
then
    return redis.call("del",key)
else
    return 0
end
"""


class RateLimitRedisClient:
    """Redis client wrapper with Lua scripts."""

    def __init__(self, client: redis.Redis) -> None:
        """Initialize the client."""
        self._client = client
        self._check_available_limit = client.register_script(_DEDUCT_SCRIPT)
        self._refund_on_fail = client.register_script(_REFUND_SCRIPT)
        self._release_lock = client.register_script(_RELEASE_LOCK_SCRIPT)

    async def deduct_if_balance(self, user_sub: str, request_bytes: int, now: float) -> int:
        """Deduct request_range bytes from users download budget if sufficienet quota is available."""
        return await self._check_available_limit(
            keys=[user_sub],
            args=[request_bytes, DOWNLOAD_CAP_BYTES, CAPPED_TIME_WINDOW, now],
        )

    async def refund(self, user_sub: str, amount: int, now: float) -> int:
        """Refund amount bytes to user_sub's budget (if window still active)."""
        return await self._refund_on_fail(
            keys=[user_sub],
            args=[amount, now],
        )

    async def release_lock(self, token_hash: str, uuid: UUID) -> int:
        """Release lock set on token_hash."""
        return await self._release_lock(
            keys=[token_hash],
            args=[uuid.bytes],
        )

    async def aclose(self) -> None:
        """Close the Redis connection and removes local script references."""
        try:
            await self._client.aclose()
        finally:
            self._check_available_limit = None
            self._refund_on_fail = None
            self._release_lock = None

    def __getattr__(self, name: str) -> Any:
        """Proxy all other redis.Redis methods (get, set, delete, aclose, …) transparently."""
        return getattr(self._client, name)
