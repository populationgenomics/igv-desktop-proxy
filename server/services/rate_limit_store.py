from pathlib import Path
from typing import Any
from uuid import UUID

import redis.asyncio as redis

from server.utils.constants import CAPPED_TIME_WINDOW_SECS, DOWNLOAD_CAP_BYTES


class RateLimitRedisClient:
    """Redis client wrapper with Lua scripts."""

    def __init__(self, client: redis.Redis) -> None:
        """Initialize the client."""
        self._client = client
        resources_path = Path(__file__).parent.parent / 'resources'
        self._check_available_limit = client.register_script(
            (resources_path / 'deduct_from_budget.lua').read_text(),
        )
        self._refund_on_fail = client.register_script(
            (resources_path / 'refund_to_budget.lua').read_text(),
        )
        self._release_lock = client.register_script(
            (resources_path / 'release_lock.lua').read_text(),
        )

    async def deduct_if_balance(self, user_sub: str, request_bytes: int, now: float) -> int:
        """Deduct request_range bytes from users download budget if sufficient download quota is available.

        Returns -> the remaining bytes if sufficient quota is available, -1 otherwise.
        """
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
