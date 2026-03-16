import logging
from typing import Any

import redis.asyncio as redis
from redis.exceptions import NoScriptError

from server.constants import DOWNLOAD_CAP_FOR_TIME_WINDOW

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

local remaining   = tonumber(redis.call('HGET', key, 'remaining_size'))
local expire_time = tonumber(redis.call('HGET', key, 'expire_time'))

if remaining ~= nil and expire_time ~= nil and (expire_time - now) > 0 then
    if (remaining - amount) >= 0 then
        local new_remaining = remaining - amount
        redis.call('HSET', key, 'remaining_size', new_remaining)
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
redis.call('HSET', key, 'remaining_size', new_remaining, 'expire_time', new_expire)
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
    redis.call('HINCRBY', key, 'remaining_size', amount)
end
return 1
"""


class RateLimitRedisClient:
    """Redis client wrapper with Lua scripts."""

    def __init__(self, client: redis.Redis, deduct_sha: str, refund_sha: str) -> None:
        """Initialize the client."""
        self._client = client
        self._deduct_sha = deduct_sha
        self._refund_sha = refund_sha

    @classmethod
    async def create(cls, client: redis.Redis) -> 'RateLimitRedisClient':
        """Register Lua scripts."""
        deduct_sha = (await client.script_load(_DEDUCT_SCRIPT)).decode()
        refund_sha = (await client.script_load(_REFUND_SCRIPT)).decode()
        return cls(client, deduct_sha, refund_sha)

    async def deduct(self, user_sub: str, request_bytes: int, now: float) -> int:
        """Deduct request_range bytes from user_sub's budget."""
        return await self._evalsha_with_fallback(
            self._deduct_sha,
            _DEDUCT_SCRIPT,
            1,
            user_sub,
            request_bytes,
            DOWNLOAD_CAP_FOR_TIME_WINDOW,
            DOWNLOAD_CAP_FOR_TIME_WINDOW,
            now,
        )

    async def refund(self, user_sub: str, amount: int, now: float) -> int:
        """Atomically refund amount bytes to user_sub's budget (if window still active)."""
        return await self._evalsha_with_fallback(
            self._refund_sha,
            _REFUND_SCRIPT,
            1,
            user_sub,
            amount,
            now,
        )

    async def _evalsha_with_fallback(self, sha: str, script: str, num_keys: int, *args: Any) -> Any:
        """Call EVALSHA; fall back to EVAL if Redis no longer has the script cached."""
        try:
            return await self._client.evalsha(sha, num_keys, *args)
        except NoScriptError:
            logging.warning(f'Script SHA {sha} not found in Redis; re-registering via EVAL.')
            return await self._client.eval(script, num_keys, *args)

    def __getattr__(self, name: str) -> Any:
        """Proxy all other redis.Redis methods (get, set, delete, aclose, …) transparently."""
        return getattr(self._client, name)
