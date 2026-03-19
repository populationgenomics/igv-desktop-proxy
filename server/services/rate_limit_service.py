import hashlib
import logging
import time
import uuid
from http import HTTPStatus

import httpx
import tenacity
from fastapi import HTTPException
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential_jitter

from server.services.redis_rate_limit_service import RateLimitRedisClient
from server.utils.constants import CPG_HOSTED_DOMAIN, LOCK_PREFIX, SUB_PREFIX, TOKEN_HASH_PREFIX
from server.utils.generic_helper import get_redis_key, is_none


class DownloadRateLimiter:
    """Implements IGV desktop proxy rate limiting logic based on a fixed window."""

    def __init__(
        self,
        redis_client: RateLimitRedisClient,
        http_client: httpx.AsyncClient,
        user_token: str,
        request_bytes: int,
    ) -> None:
        """Initialize rate limiter clients and request specifics."""
        self.redis_client = redis_client
        self.http_client = http_client

        self.user_token = user_token
        self.request_bytes = request_bytes  # bytes; IGV typically requests 512 KB per request

        self.user_sub: str | None = None

    async def check_user_limit(self) -> bool:
        """Validate user and check their download limits."""
        token_hash = hashlib.sha256(self.user_token.encode('utf-8')).hexdigest()

        try:
            self.user_sub = await self.get_authenticated_user_id(token_hash)
        except tenacity.RetryError as err:
            logging.error(f'Failed to get user info after retrying. {err}')
            self.user_sub = None

        if self.user_sub is None:
            raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail=HTTPStatus.UNAUTHORIZED.phrase)

        return await self.evaluate_download_limits()

    async def refund(self) -> None:
        """Return the reserved bytes to the download budget after a failed GCS request."""
        if self.user_sub is None:
            return

        now = time.time()
        try:
            assert self.user_sub is not None
            key = get_redis_key(SUB_PREFIX, self.user_sub)
            await self.redis_client.refund(key, self.request_bytes, now)
        except Exception as exc:  # noqa: BLE001
            logging.error(f'Failed to refund rate-limit quota for user {self.user_sub}: {exc}')

    @retry(retry=retry_if_result(is_none), wait=wait_exponential_jitter(initial=1, max=16), stop=stop_after_attempt(5))
    async def get_authenticated_user_id(self, token_hash: str) -> str | None:
        """Check cache or external service to validate the user access token."""
        # Already cached.
        token_key = get_redis_key(TOKEN_HASH_PREFIX, token_hash)
        user_sub = await self.redis_client.get(token_key)
        if user_sub is not None:
            return user_sub

        lock_key = get_redis_key(LOCK_PREFIX, token_hash)

        # Try to acquire the lock
        # Only one request is allowed to invoke userinfo endpoint if there are concurrent requests with the same token
        request_uuid = uuid.uuid4()
        acquired = await self.redis_client.set(lock_key, request_uuid.bytes, nx=True, ex=10)

        if acquired:
            # recheck after lock acquisition
            user_sub = await self.redis_client.get(token_key)
            if user_sub is not None:
                return user_sub
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
                            nx=True,
                        )  # expire this key after 1-hour. Mirror expiry time of the access token
                        return user_sub
                    raise HTTPException(HTTPStatus.UNAUTHORIZED)
            finally:
                await self.redis_client.release_lock(lock_key, request_uuid)

        return None

    async def evaluate_download_limits(self) -> bool:
        """Deduct the requested byte size from the user's download budget."""
        now = time.time()

        assert self.user_sub is not None
        sub_key = get_redis_key(SUB_PREFIX, self.user_sub)
        remaining_bytes = await self.redis_client.deduct_if_balance(sub_key, self.request_bytes, now)

        return remaining_bytes >= 0

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
