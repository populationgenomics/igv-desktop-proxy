import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from http import HTTPStatus

import httpx
import tenacity
from fastapi import HTTPException
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential_jitter

from server.services.rate_limit_store import RateLimitRedisClient
from server.utils.constants import (
    LOCK_PREFIX,
    STATS_PREFIX,
    SUB_PREFIX,
    TOKEN_CACHE_MAX_TTL_SECS,
    TOKEN_HASH_PREFIX,
    TOKENINFO_URL,
)
from server.utils.helpers import is_none

# tokeninfo serialises every value as a string, so `email_verified` arrives as 'true', not True.
# Accept the genuine boolean too; anything else — 'false', '', absent — is not a verified email.
TRUTHY_CLAIM_VALUES = (True, 'true', 'True')


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


def identity_cache_ttl(token_info: dict) -> int:
    """Return how long to cache an identity: the token's own remaining life, capped at an hour."""
    try:
        expires_in = int(token_info.get('expires_in', TOKEN_CACHE_MAX_TTL_SECS))
    except (TypeError, ValueError):
        expires_in = TOKEN_CACHE_MAX_TTL_SECS

    return max(1, min(expires_in, TOKEN_CACHE_MAX_TTL_SECS))


class DownloadRateLimiter:
    """Resolves the caller's identity and implements fixed-window download rate limiting."""

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
        self.request_bytes = request_bytes  # bytes; IGV typically requests 512 KB per request (CRAM, BAM)

        self.user_sub: str | None = None
        self.user_email: str | None = None

    async def resolve_user(self) -> str:
        """Establish who is calling, from their access token, and return their email address.

        Sets user_sub (the key for budget and stats) and user_email (the key for the access map);
        the email is returned because it is what the caller needs next, to authorize. Raises 401 if
        either is unavailable — the proxy now acts on the caller's behalf with its own credentials,
        so an unidentified caller can never be served.
        """
        token_hash = hashlib.sha256(self.user_token.encode('utf-8')).hexdigest()

        try:
            identity = await self.get_authenticated_identity(token_hash)
        except tenacity.RetryError as err:
            logging.error(f'Failed to get token info after retrying. {err}')
            identity = None

        if identity is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED)

        self.user_sub = identity['sub']
        self.user_email = identity['email']
        return self.user_email

    @retry(retry=retry_if_result(is_none), wait=wait_exponential_jitter(initial=1, max=16), stop=stop_after_attempt(5))
    async def get_authenticated_identity(self, token_hash: str) -> dict[str, str] | None:
        """Check cache or Google's tokeninfo endpoint to resolve the user's access token."""
        # Already cached.
        token_key = f'{TOKEN_HASH_PREFIX}:{token_hash}'
        identity = decode_cached_identity(await self.redis_client.get(token_key))
        if identity is not None:
            return identity

        lock_key = f'{LOCK_PREFIX}:{token_hash}'

        # Try to acquire the lock
        # Only one request is allowed to invoke tokeninfo endpoint if there are concurrent requests with the same token
        request_uuid = uuid.uuid4()
        acquired = await self.redis_client.set(lock_key, request_uuid.bytes, nx=True, ex=10)

        if acquired:
            # recheck after lock acquisition
            identity = decode_cached_identity(await self.redis_client.get(token_key))
            if identity is not None:
                return identity
            try:
                token_info = await self.fetch_token_info()
                if token_info is None:
                    # A transport failure, not a verdict on the token. Worth another attempt.
                    return None

                # Raises 401 on a token this proxy must not act on. Nothing is cached in that case:
                # the only value ever written here is a complete, verified identity.
                identity = self.verify_token_info(token_info)
                await self.redis_client.set(
                    token_key,
                    json.dumps(identity),
                    ex=identity_cache_ttl(token_info),
                    nx=True,
                )
                return identity
            finally:
                await self.redis_client.release_lock(lock_key, request_uuid)

        return None

    async def fetch_token_info(self) -> dict | None:
        """Fetch token info from Google's tokeninfo endpoint.

        tokeninfo rather than userinfo because one call returns the identity claims *and*
        `expires_in`, which bounds how long the resolved identity may be cached. userinfo says
        nothing about the token's own remaining lifetime.
        """
        query_params = {'access_token': self.user_token}

        try:
            response = await self.http_client.get(TOKENINFO_URL, params=query_params)
            return response.json()
        except (httpx.HTTPError, ValueError) as err:
            logging.error(f'Failed to fetch token info. {err}')
            return None

    def verify_token_info(self, token_info: dict) -> dict[str, str]:
        """Return the {sub, email} identity this token proves, or raise 401.

        Every branch here refuses rather than degrades: the access map is keyed by email, so an
        email the proxy cannot vouch for is worse than no email at all.

        Deliberately not checked: `aud`, the OAuth client the token was issued to. Anyone listed in
        the access map may read the buckets they are listed against with any Google token they
        hold, not only one minted by IGV. Reaching the data outside IGV is not intended, but it is
        accepted — the access map, not the calling application, is the control.
        """
        if 'error' in token_info:
            logging.warning(f'tokeninfo rejected the access token: {token_info.get("error")}')
            raise HTTPException(HTTPStatus.UNAUTHORIZED)

        email = token_info.get('email')
        if not email:
            logging.error(
                'tokeninfo returned no email claim. The user consented without the email scope, or — if this '
                'is happening for every request — IGV is not being provisioned with the email scope at all.',
            )
            raise HTTPException(HTTPStatus.UNAUTHORIZED)

        email_verified = token_info.get('email_verified')
        if email_verified not in TRUTHY_CLAIM_VALUES:
            logging.error(f'Refusing an unverified email address. email_verified={email_verified!r}')
            raise HTTPException(HTTPStatus.UNAUTHORIZED)

        sub = token_info.get('sub')
        if not sub:
            logging.error('tokeninfo returned no sub claim; download budget and stats cannot be attributed.')
            raise HTTPException(HTTPStatus.UNAUTHORIZED)

        return {'sub': sub, 'email': email}

    async def evaluate_download_limits(self) -> bool:
        """Deduct the requested byte size from the user's download budget if quota not exceeded."""
        now = time.time()

        assert self.user_sub is not None
        sub_key = f'{SUB_PREFIX}:{self.user_sub}'
        remaining_bytes = await self.redis_client.deduct_if_balance(sub_key, self.request_bytes, now)

        return remaining_bytes >= 0

    async def record_download_stats(self, bucket: str) -> None:
        """Record successfully streamed bytes into the daily download stats."""
        if self.user_sub is None:
            return
        date_str = datetime.now(UTC).strftime('%Y-%m-%d')
        stats_key = f'{STATS_PREFIX}:{self.user_sub}:{bucket}:{date_str}'
        await self.redis_client.increment_download_stats(stats_key=stats_key, bytes_count=self.request_bytes)

    async def refund(self) -> None:
        """Return the reserved bytes to the download budget after a failed GCS request."""
        if self.user_sub is None:
            return

        now = time.time()
        assert self.user_sub is not None
        key = f'{SUB_PREFIX}:{self.user_sub}'
        await self.redis_client.refund(key, self.request_bytes, now)
