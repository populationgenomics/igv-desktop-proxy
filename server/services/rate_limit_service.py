import hashlib
import logging
import time
import uuid

import httpx
import tenacity
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential_jitter

from server.services.redis_rate_limit_service import RateLimitRedisClient
from server.utils.constants import CPG_HOSTED_DOMAIN
from server.utils.util import is_none


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

        self.remaining_bytes: int | None = None

    async def check_user_limit(self) -> bool:
        """Validate user and check their download limits."""
        token_hash = hashlib.sha256(self.user_token.encode('utf-8')).hexdigest()

        try:
            self.user_sub = await self.get_authenticated_user_id(token_hash)
        except tenacity.RetryError as err:
            logging.error(f'Failed to get user info after retrying. {err}')
            self.user_sub = None

        if self.user_sub is None:
            return False  # TODO raise an exception here

        return await self.evaluate_download_limits()

    async def refund(self) -> None:
        """Return the reserved bytes to the download budget after a failed GCS request."""
        if self.user_sub is None or self.remaining_bytes is None:
            return

        now = time.time()
        try:
            assert self.user_sub is not None
            key = f'sub:{self.user_sub}'
            await self.redis_client.refund(key, self.request_bytes, now)
        except Exception as exc:  # noqa: BLE001
            logging.error(f'Failed to refund rate-limit quota for user {self.user_sub}: {exc}')

    @retry(retry=retry_if_result(is_none), wait=wait_exponential_jitter(initial=1, max=16), stop=stop_after_attempt(5))
    async def get_authenticated_user_id(self, token_hash: str) -> str | None:
        """Check cache or external service to validate the user access token."""
        # Already cached.
        token_key = f'token_hash:{token_hash}'
        user_sub = await self.redis_client.get(token_key)
        if user_sub is not None:
            return user_sub

        lock_key = f'lock:{token_hash}'

        # Try to acquire the lock
        # Only one request is allowed to invoke userinfo endpoint if there are concurrent requests with the same token
        request_uuid = uuid.uuid4()
        acquired = await self.redis_client.set(lock_key, request_uuid.bytes, nx=True, ex=10)

        if acquired:
            try:
                user_info = await self.fetch_user_info()
                if user_info:
                    user_sub = user_info.get('sub')
                    hd = user_info.get('hd')  # validate domain membership

                    if user_sub and hd == CPG_HOSTED_DOMAIN:
                        await self.redis_client.set(
                            token_key,
                            user_sub,
                            ex=3600,
                        )  # expire this key after 1-hour. Mirror expiry time of the access token
                        await self.redis_client.set()
                        return user_sub
            finally:
                await self.redis_client.release_lock(lock_key, request_uuid)

        return None

    async def evaluate_download_limits(self) -> bool:
        """Deduct the requested byte size from the user's download budget."""
        now = time.time()

        assert self.user_sub is not None
        sub_key = f'sub:{self.user_sub}'
        result = await self.redis_client.deduct_if_balance(sub_key, self.request_bytes, now)

        if result < 0:
            return False  # cap exceeded or single request > cap

        self.remaining_bytes = int(result)
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
