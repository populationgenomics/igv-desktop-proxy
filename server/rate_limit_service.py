import hashlib
import logging
import time

import httpx
import redis.asyncio as redis

from server.constants import DOWNLOAD_CAP_FOR_TIME_WINDOW, HOSTED_DOMAIN


class DownloadRateLimiter:
    """Handles rate limiting logic."""

    def __init__(
        self,
        redis_client: redis.Redis,
        http_client: httpx.AsyncClient,
        user_token: str,
        request_range: str,
    ) -> None:
        """Initialize clients."""
        self.redis_client = redis_client
        self.http_client = http_client

        self.user_token = user_token.split(' ')[1]

        range_parts = request_range.replace('bytes=', '').split('-')
        start_byte = int(range_parts[0])
        end_byte = int(range_parts[1])
        self.request_range = end_byte - start_byte + 1  # As of now, IGV requests 512KB bytes per request

        self.user_sub: str | None = None

        # rate limit related
        self.remaining_size: int | None = None
        self.expire_time: float | None = None

    async def check_user_limit(self) -> bool:
        """Validate user and check their download limits."""
        token_hash = hashlib.sha256(self.user_token.encode('utf-8')).hexdigest()
        self.user_sub = await self._get_authenticated_user_id(token_hash)

        if self.user_sub is None:
            return False

        return await self._evaluate_download_limits()

    async def _get_authenticated_user_id(self, token_hash: str) -> str | None:
        """Check cache or external service to validate the user access token."""
        # check first in the cache
        user_sub = await self.redis_client.get(token_hash)
        if user_sub is not None:
            return user_sub

        # else invoke userinfo endpoint
        user_info = await self.fetch_user_info()
        if user_info:
            user_sub = user_info.get('sub')
            hd = user_info.get('hd')  # extra validation to check if the user belong to the CPG hosted domain.

            if user_sub and hd == HOSTED_DOMAIN:
                await self.redis_client.set(
                    token_hash,
                    user_sub,
                    ex=3600,
                )  # expire this key after 1-hour, the user access token is valid for 1h.
                return user_sub

        return None

    async def _evaluate_download_limits(self) -> bool:
        """Evaluate if the user has enough capacity in their current time window."""
        user_download_info = await self.redis_client.get(self.user_sub)
        now = time.time()

        if user_download_info:
            expire_time = user_download_info.get('expire_time')
            remaining_size = user_download_info.get('remaining_size')

            # If within the active time window
            if (expire_time - now) > 0:
                if (remaining_size - self.request_range) >= 0:
                    self.remaining_size = remaining_size - self.request_range
                    self.expire_time = expire_time
                    return True
                return False  # Cap exceeded

        # User is new, or previous window expired. Grant new window.
        self.remaining_size = DOWNLOAD_CAP_FOR_TIME_WINDOW - self.request_range  # TODO check range
        self.expire_time = now
        return True

    async def fetch_user_info(self) -> dict | None:
        """Fetch user info."""
        url = 'https://www.googleapis.com/oauth2/v3/userinfo'

        query_params = {'access_token': self.user_token}

        try:
            response = await self.http_client.get(url, params=query_params)
            return response.json()
        except httpx.HTTPError as err:
            logging.info(f'Failed to fetch user info. {err}')
            return None
