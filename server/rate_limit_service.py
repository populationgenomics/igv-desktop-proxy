import asyncio
import hashlib
import logging
import time

import httpx

from server.constants import HOSTED_DOMAIN, LOCK_POLL_ATTEMPTS, LOCK_POLL_SLEEP_S
from server.redis_client import RateLimitRedisClient


class DownloadRateLimiter:
    """Handles rate limiting logic with concurrency-safe Redis operations."""

    def __init__(
        self,
        redis_client: RateLimitRedisClient,
        http_client: httpx.AsyncClient,
        user_token: str,
        request_bytes: int,
    ) -> None:
        """Initialize clients."""
        self.redis_client = redis_client
        self.http_client = http_client

        self.user_token = user_token
        self.request_bytes = request_bytes  # bytes; IGV typically requests 512 KB per request

        self.user_sub: str | None = None

        self.remaining_size: int | None = None  # set after a successful deduction

    async def check_user_limit(self) -> bool:
        """Validate user and check their download limits."""
        token_hash = hashlib.sha256(self.user_token.encode('utf-8')).hexdigest()
        self.user_sub = await self._get_authenticated_user_id(token_hash)

        if self.user_sub is None:
            return False

        return await self._evaluate_download_limits()

    async def refund(self) -> None:
        """Return the reserved bytes to the download budget after a failed GCS request."""
        if self.user_sub is None or self.remaining_size is None:
            return

        now = time.time()
        try:
            assert self.user_sub is not None
            await self.redis_client.refund(self.user_sub, self.request_bytes, now)
        except Exception as exc:  # noqa: BLE001
            logging.error(f'Failed to refund rate-limit quota for user {self.user_sub}: {exc}')

    async def _get_authenticated_user_id(self, token_hash: str) -> str | None:
        """Check cache or external service to validate the user access token."""
        # Already cached.
        user_sub = await self.redis_client.get(token_hash)
        if user_sub is not None:
            return user_sub

        lock_key = f'lock:token:{token_hash}'

        # Try to acquire the lock.
        acquired = await self.redis_client.set(lock_key, '1', nx=True, ex=15)

        if acquired:
            try:
                user_info = await self.fetch_user_info()
                if user_info:
                    user_sub = user_info.get('sub')
                    hd = user_info.get('hd')  # validate domain membership

                    if user_sub and hd == HOSTED_DOMAIN:
                        await self.redis_client.set(
                            token_hash,
                            user_sub,
                            ex=3600,
                        )  # expire this key after 1-hour. Mirror expiry time of the access token
                        return user_sub
            finally:
                await self.redis_client.delete(lock_key)

            return None

        # TODO back off and retry
        for _ in range(LOCK_POLL_ATTEMPTS):
            await asyncio.sleep(LOCK_POLL_SLEEP_S)
            user_sub = await self.redis_client.get(token_hash)
            if user_sub is not None:
                return user_sub

        return None

    async def _evaluate_download_limits(self) -> bool:
        """Deduct the request size from the user's download budget."""
        now = time.time()

        # user_sub is always set before this method is called.
        assert self.user_sub is not None
        result = await self.redis_client.deduct(self.user_sub, self.request_bytes, now)

        if result < 0:
            return False  # cap exceeded or single request > cap

        self.remaining_size = int(result)
        return True

    async def fetch_user_info(self) -> dict | None:
        """Fetch user info from Google's userinfo endpoint."""
        url = 'https://www.googleapis.com/oauth2/v3/userinfo'

        query_params = {'access_token': self.user_token}

        try:
            response = await self.http_client.get(url, params=query_params)
            return response.json()
        except httpx.HTTPError as err:
            logging.error(f'Failed to fetch user info. {err}')
            return None
