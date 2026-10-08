import hashlib
import json
import logging
import uuid
from http import HTTPStatus

import httpx
import tenacity
from fastapi import HTTPException
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential_jitter

from server.services.rate_limit_store import RateLimitRedisClient
from server.utils.constants import (
    LOCK_PREFIX,
    TOKEN_CACHE_TTL_SECS,
    TOKEN_HASH_PREFIX,
    USERINFO_URL,
)
from server.utils.helpers import is_none


def decode_cached_identity(cached: str | None) -> dict[str, str] | None:
    """Return the cached {sub, email} identity, or None if there is nothing usable cached."""
    if cached is None:
        return None

    try:
        identity = json.loads(cached)
    except ValueError:
        logging.warning('Discarding an unparseable cached identity.')
        return None

    if not isinstance(identity, dict) or not identity.get('sub') or not identity.get('email'):
        logging.warning('Discarding an incomplete cached identity.')
        return None

    return {'sub': identity['sub'], 'email': identity['email']}


def verify_user_info(user_info: dict) -> dict[str, str]:
    """Return the {sub, email} identity this token proves, or raise 401."""
    if 'error' in user_info:
        logging.warning(f'userinfo rejected the access token: {user_info.get("error")}')
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    email = user_info.get('email')
    if not email:
        logging.error(
            'userinfo returned no email claim. The user consented without the email scope, or — if this '
            'is happening for every request — IGV is not being provisioned with the email scope at all.',
        )
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    # userinfo sends a boolean, tokeninfo the string 'true'; accept either so the endpoint can be swapped
    email_verified = user_info.get('email_verified')
    if email_verified is not True and email_verified != 'true':
        logging.error(f'Refusing an unverified email address. email_verified={email_verified!r}')
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    sub = user_info.get('sub')
    if not sub:
        logging.error('userinfo returned no sub claim; download budget and stats cannot be attributed.')
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    return {'sub': sub, 'email': email}


class UserIdentityResolver:
    """Resolves the caller's identity from their Google access token."""

    def __init__(
        self,
        redis_client: RateLimitRedisClient,
        http_client: httpx.AsyncClient,
        user_token: str,
    ) -> None:
        """Initialize identity cache and userinfo clients."""
        self.redis_client = redis_client
        self.http_client = http_client
        self.user_token = user_token

    async def resolve(self) -> dict[str, str]:
        """Return the caller's {sub, email} identity from their access token, or raise 401."""
        token_hash = hashlib.sha256(self.user_token.encode('utf-8')).hexdigest()

        try:
            identity = await self.get_authenticated_identity(token_hash)
        except tenacity.RetryError as err:
            logging.error(f'Failed to get token info after retrying. {err}')
            identity = None

        if identity is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED)

        return identity

    @retry(retry=retry_if_result(is_none), wait=wait_exponential_jitter(initial=1, max=16), stop=stop_after_attempt(5))
    async def get_authenticated_identity(self, token_hash: str) -> dict[str, str] | None:
        """Check cache or Google's userinfo endpoint to resolve the user's access token."""
        # Already cached.
        token_key = f'{TOKEN_HASH_PREFIX}:{token_hash}'
        identity = decode_cached_identity(await self.redis_client.get(token_key))
        if identity is not None:
            return identity

        lock_key = f'{LOCK_PREFIX}:{token_hash}'

        # Try to acquire the lock
        # Only one request is allowed to invoke userinfo endpoint if there are concurrent requests with the same token
        request_uuid = uuid.uuid4()
        acquired = await self.redis_client.set(lock_key, request_uuid.bytes, nx=True, ex=10)

        if acquired:
            # recheck after lock acquisition
            identity = decode_cached_identity(await self.redis_client.get(token_key))
            if identity is not None:
                return identity
            try:
                user_info = await self.fetch_user_info()
                if user_info is None:
                    # A transport failure, not a verdict on the token. Worth another attempt.
                    return None

                # Raises 401 before caching, so only a verified identity is ever cached
                identity = verify_user_info(user_info)
                await self.redis_client.set(
                    token_key,
                    json.dumps(identity),
                    ex=TOKEN_CACHE_TTL_SECS,
                    nx=True,
                )
                return identity
            finally:
                await self.redis_client.release_lock(lock_key, request_uuid)

        return None

    async def fetch_user_info(self) -> dict | None:
        """Fetch user info from Google's userinfo endpoint, or None on a failure worth retrying."""
        query_params = {'access_token': self.user_token}

        try:
            response = await self.http_client.get(USERINFO_URL, params=query_params)
            status = response.status_code
            if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                # Throttled or down, not a verdict on the token. Worth another attempt.
                logging.error(f'userinfo returned {status}.')
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as err:
            logging.error(f'Failed to fetch user info. {err}')
            return None
