from typing import Any
from uuid import UUID

import redis.asyncio as redis

from server.resources.deduct_from_budget_lua_script import DEDUCT_LUA_SCRIPT
from server.resources.refund_to_budget_lua_script import REFUND_LUA_SCRIPT
from server.resources.release_lock_lua_script import RELEASE_LOCK_LUA_SCRIPT
from server.utils.constants import CAPPED_TIME_WINDOW_SECS, DOWNLOAD_CAP_BYTES


class RateLimitRedisClient:
    """Redis client wrapper with Lua scripts."""

    def __init__(self, client: redis.Redis) -> None:
        """Initialize the client."""
        self._client = client
        self._check_available_limit = client.register_script(DEDUCT_LUA_SCRIPT)
        self._refund_on_fail = client.register_script(REFUND_LUA_SCRIPT)
        self._release_lock = client.register_script(RELEASE_LOCK_LUA_SCRIPT)

    async def deduct_if_balance(self, user_sub: str, request_bytes: int, now: float) -> int:
        """Deduct request_range bytes from users download budget if sufficient quota is available."""
        if request_bytes > DOWNLOAD_CAP_BYTES:
            return -1

        return await self._check_available_limit(
            keys=[user_sub],
            args=[request_bytes, DOWNLOAD_CAP_BYTES, CAPPED_TIME_WINDOW_SECS, now],
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
